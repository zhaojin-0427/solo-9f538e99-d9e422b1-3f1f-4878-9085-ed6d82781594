"""端到端测试：险情点脱敏汇总 API（POST /summary）。"""
from __future__ import annotations

import importlib
import math

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import shape


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """每个用例使用独立临时 SQLite 文件，模拟全新部署。"""
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "test.db"))
    import app.config as config
    import app.db as dbmod
    import app.main as main
    # reload 顺序：config 先、main 后，确保 main 中的 Database 落到临时库
    importlib.reload(config)
    importlib.reload(dbmod)
    importlib.reload(main)
    with TestClient(main.app) as c:
        yield c


def poly(coords):
    return {"type": "Polygon", "coordinates": [coords]}


# 2x2 四格：流域 [0,4]x[0,4]
# A 左上 [0,2]x[2,4] / B 右上 [2,4]x[2,4] / C 左下 [0,2]x[0,2] / D 右下 [2,4]x[0,2]
# 共享边：A-B、A-C、B-D、C-D；A-D、B-C 仅共点 (2,2)，不算共享边
WATERSHED = poly([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
A = poly([(0, 2), (2, 2), (2, 4), (0, 4), (0, 2)])
B = poly([(2, 2), (4, 2), (4, 4), (2, 4), (2, 2)])
C = poly([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])
D = poly([(2, 0), (4, 0), (4, 2), (2, 2), (2, 0)])


def publish4(expected=None):
    return {
        "expected_version": expected,
        "watershed": WATERSHED,
        "grids": [
            {"grid_id": "A", "geometry": A},
            {"grid_id": "B", "geometry": B},
            {"grid_id": "C", "geometry": C},
            {"grid_id": "D", "geometry": D},
        ],
    }


def pt(pid, lon, lat):
    return {"point_id": pid, "lon": lon, "lat": lat}


# ---------- 汇总主流程 ----------

def test_summary_basic_regions_and_unpublished(client):
    """合格区合并公布；边界点与域外点计入未发布总数。"""
    assert client.post("/publish", json=publish4()).status_code == 200
    r = client.post("/summary", json={
        "k": 2,
        "points": [
            pt("a1", 1.0, 3.0),            # A 内
            pt("b1", 3.0, 3.0),            # B 内
            pt("b2", 3.5, 3.5),            # B 内
            pt("b3", 2.5, 2.5),            # B 内
            pt("d1", 3.0, 1.0),            # D 内
            pt("edge", 2.0, 2.0),          # 四格角点：边界，不参与汇总
            pt("out", 9.0, 9.0),           # 域外
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["is_current"] is True and body["k"] == 2
    # 最优：{A,B,D} 共 5 点（A1+B3+D1），面积 12
    assert len(body["regions"]) == 1
    region = body["regions"][0]
    assert region["grid_ids"] == ["A", "B", "D"]
    assert region["point_count"] == 5
    assert region["geometry"]["type"] == "Polygon"
    assert shape(region["geometry"]).area == pytest.approx(12.0)
    # 未发布总数 = 边界 1 + 域外 1
    assert body["unpublished_total"] == 2


def test_summary_tie_break_prefers_lexicographically_smaller(client):
    """点数、面积都相同时，按各区网格编号序列字典序取最小。"""
    client.post("/publish", json=publish4())
    # A、B 各 2 点：{A}+{B} 与 {A,B} 点数面积全同 → (("A",),("B",)) 更小
    r = client.post("/summary", json={
        "k": 2,
        "points": [
            pt("a1", 0.5, 3.0), pt("a2", 1.5, 3.5),
            pt("b1", 2.5, 3.0), pt("b2", 3.5, 3.5),
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert [reg["grid_ids"] for reg in body["regions"]] == [["A"], ["B"]]
    assert [reg["point_count"] for reg in body["regions"]] == [2, 2]
    assert body["unpublished_total"] == 0


def test_summary_corner_touch_grids_cannot_merge(client):
    """仅共点（零长度接触）的网格不算共享边，不能合并成区。"""
    watershed = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
            [[[1, 1], [2, 1], [2, 2], [1, 2], [1, 1]]],
        ],
    }
    grids = [
        {"grid_id": "P", "geometry": poly([(0, 0), (1, 0), (1, 1), (0, 1), (0, 0)])},
        {"grid_id": "Q", "geometry": poly([(1, 1), (2, 1), (2, 2), (1, 2), (1, 1)])},
    ]
    r = client.post("/publish", json={
        "expected_version": None, "watershed": watershed, "grids": grids,
    })
    assert r.status_code == 200, r.text
    # 两格各 1 点，k=2：若能合并则够 k，但 P、Q 仅在 (1,1) 共点 → 无合格区
    r = client.post("/summary", json={
        "k": 2,
        "points": [pt("p1", 0.5, 0.5), pt("q1", 1.5, 1.5)],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["regions"] == []
    assert body["unpublished_total"] == 2


def test_summary_points_outrank_area_then_area_minimized(client):
    """先比点数，再比面积。"""
    # 链式两格：G1 面积 4，G2 面积 1
    watershed = poly([(0, 0), (5, 0), (5, 1), (0, 1), (0, 0)])
    g1 = poly([(0, 0), (4, 0), (4, 1), (0, 1), (0, 0)])
    g2 = poly([(4, 0), (5, 0), (5, 1), (4, 1), (4, 0)])
    r = client.post("/publish", json={
        "expected_version": None,
        "watershed": watershed,
        "grids": [{"grid_id": "G1", "geometry": g1},
                  {"grid_id": "G2", "geometry": g2}],
    })
    assert r.status_code == 200, r.text

    # 面积最小化：G1 有 2 点、G2 空 → {G1}(2点,面积4) 胜过 {G1,G2}(2点,面积5)
    r = client.post("/summary", json={
        "k": 2, "points": [pt("p1", 1.0, 0.5), pt("p2", 2.0, 0.5)],
    })
    body = r.json()
    assert [reg["grid_ids"] for reg in body["regions"]] == [["G1"]]
    assert shape(body["regions"][0]["geometry"]).area == pytest.approx(4.0)

    # 点数优先：G1 有 2 点、G2 有 1 点 → {G1,G2}(3点,面积5) 胜过 {G1}(2点,面积4)
    r = client.post("/summary", json={
        "k": 2,
        "points": [pt("p1", 1.0, 0.5), pt("p2", 2.0, 0.5), pt("p3", 4.5, 0.5)],
    })
    body = r.json()
    assert [reg["grid_ids"] for reg in body["regions"]] == [["G1", "G2"]]
    assert body["regions"][0]["point_count"] == 3
    assert body["unpublished_total"] == 0


def test_summary_region_capped_at_three_grids(client):
    """发布区至多 3 格：4 格链各 1 点、k=4 时无合格区；k=3 时取三连格。"""
    watershed = poly([(0, 0), (4, 0), (4, 1), (0, 1), (0, 0)])
    grids = [
        {"grid_id": f"H{i}", "geometry": poly(
            [(i, 0), (i + 1, 0), (i + 1, 1), (i, 1), (i, 0)]
        )}
        for i in range(4)
    ]
    r = client.post("/publish", json={
        "expected_version": None, "watershed": watershed, "grids": grids,
    })
    assert r.status_code == 200, r.text
    points = [pt(f"p{i}", i + 0.5, 0.5) for i in range(4)]

    # k=4：若允许 4 格区则合格，但上限 3 格 → 无合格区，只返回总数
    r = client.post("/summary", json={"k": 4, "points": points})
    body = r.json()
    assert body["regions"] == []
    assert body["unpublished_total"] == 4

    # k=3：{H1,H2,H3} 与 {H2,H3,H4} 同分，编号序列小者胜
    r = client.post("/summary", json={"k": 3, "points": points})
    body = r.json()
    assert [reg["grid_ids"] for reg in body["regions"]] == [["H1", "H2", "H3"]]
    assert body["regions"][0]["point_count"] == 3
    assert body["unpublished_total"] == 1


def test_summary_no_qualified_region_returns_only_total(client):
    """无合格区时 regions 为空，未发布总数等于全部点数。"""
    client.post("/publish", json=publish4())
    r = client.post("/summary", json={
        "k": 5,
        "points": [pt("a1", 1.0, 3.0), pt("b1", 3.0, 3.0), pt("d1", 3.0, 1.0)],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["regions"] == []
    assert body["unpublished_total"] == 3


# ---------- 版本固定与快照 ----------

def test_summary_pinned_version_immune_to_republish(client):
    """指定旧版的结果不受网格改版影响；省略版号时固定当前版。"""
    left = poly([(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)])
    right = poly([(2, 0), (4, 0), (4, 4), (2, 4), (2, 0)])
    r = client.post("/publish", json={
        "expected_version": None,
        "watershed": WATERSHED,
        "grids": [{"grid_id": "L", "geometry": left},
                  {"grid_id": "R", "geometry": right}],
    })
    assert r.status_code == 200
    points = [pt("p1", 0.5, 0.5), pt("p2", 1.0, 1.0), pt("p3", 1.5, 1.5)]

    r1 = client.post("/summary", json={"version": 1, "k": 2, "points": points})
    assert [reg["grid_ids"] for reg in r1.json()["regions"]] == [["L"]]
    assert r1.json()["is_current"] is True

    # 改版：上下两格 B/T，当前版变为 2
    bottom = poly([(0, 0), (4, 0), (4, 2), (0, 2), (0, 0)])
    top = poly([(0, 2), (4, 2), (4, 4), (0, 4), (0, 2)])
    r = client.post("/publish", json={
        "expected_version": 1,
        "watershed": WATERSHED,
        "grids": [{"grid_id": "B", "geometry": bottom},
                  {"grid_id": "T", "geometry": top}],
    })
    assert r.status_code == 200

    # 指定旧版 1：结果与改版前完全一致（仅 is_current 变为 False）
    r2 = client.post("/summary", json={"version": 1, "k": 2, "points": points})
    assert r2.status_code == 200
    assert r2.json()["regions"] == r1.json()["regions"]
    assert r2.json()["unpublished_total"] == r1.json()["unpublished_total"]
    assert r2.json()["is_current"] is False

    # 省略版号：固定当前版 2，三点全部落在 B
    r3 = client.post("/summary", json={"k": 2, "points": points})
    assert r3.json()["version"] == 2 and r3.json()["is_current"] is True
    assert [reg["grid_ids"] for reg in r3.json()["regions"]] == [["B"]]


def test_summary_rejects_version_with_more_than_8_grids(client):
    """仅支持 2~8 格的版本；9 格版本返回 422。"""
    grids = [
        {"grid_id": f"G{i}{j}", "geometry": poly(
            [(i, j), (i + 1, j), (i + 1, j + 1), (i, j + 1), (i, j)]
        )}
        for i in range(3) for j in range(3)
    ]
    r = client.post("/publish", json={
        "expected_version": None,
        "watershed": poly([(0, 0), (3, 0), (3, 3), (0, 3), (0, 0)]),
        "grids": grids,
    })
    assert r.status_code == 200, r.text
    r = client.post("/summary", json={
        "k": 2, "points": [pt("p1", 0.5, 0.5), pt("p2", 1.5, 1.5)],
    })
    assert r.status_code == 422
    assert r.json()["error"] == "UNSUPPORTED_GRID_COUNT"


# ---------- 输入校验 ----------

def test_summary_validation_errors(client):
    client.post("/publish", json=publish4())
    one_point = [pt("p1", 1.0, 3.0)]

    # 点数越界：0 个或 31 个
    assert client.post("/summary", json={"k": 2, "points": []}).status_code == 422
    too_many = [pt(f"p{i}", 1.0, 3.0) for i in range(31)]
    assert client.post("/summary", json={"k": 2, "points": too_many}).status_code == 422

    # k 越界
    assert client.post("/summary", json={"k": 1, "points": one_point}).status_code == 422
    assert client.post("/summary", json={"k": 6, "points": one_point}).status_code == 422

    # 编号重复
    r = client.post("/summary", json={
        "k": 2, "points": [pt("dup", 1.0, 3.0), pt("dup", 3.0, 3.0)],
    })
    assert r.status_code == 422
    assert r.json()["error"] == "DUPLICATE_POINT_ID"

    # 坐标必须有限：NaN / Infinity 拒绝
    r = client.post("/summary", json={
        "k": 2, "points": [pt("p1", math.nan, 3.0)],
    })
    assert r.status_code == 422
    r = client.post("/summary", json={
        "k": 2, "points": [pt("p1", 1.0, math.inf)],
    })
    assert r.status_code == 422

    # 编号缺失
    r = client.post("/summary", json={"k": 2, "points": [{"lon": 1.0, "lat": 3.0}]})
    assert r.status_code == 422

    # 版本不存在
    r = client.post("/summary", json={"version": 9, "k": 2, "points": one_point})
    assert r.status_code == 404
    assert r.json()["error"] == "VERSION_NOT_FOUND"


def test_summary_points_limit_30_ok(client):
    client.post("/publish", json=publish4())
    points = [pt(f"p{i}", 1.0, 3.0) for i in range(30)]
    r = client.post("/summary", json={"k": 5, "points": points})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [reg["grid_ids"] for reg in body["regions"]] == [["A"]]
    assert body["regions"][0]["point_count"] == 30


def test_summary_no_published_version(client):
    r = client.post("/summary", json={"k": 2, "points": [pt("p1", 1.0, 1.0)]})
    assert r.status_code == 404
    assert r.json()["error"] == "NO_PUBLISHED_VERSION"


# ---------- 脱敏约束 ----------

def test_summary_response_has_no_point_or_cell_details(client):
    """响应不得包含点号、坐标或未发布格的单格计数。"""
    # 两块互不相邻的流域：A 格 2 点可公布，X 格 1 点不足 k 且无邻格可并
    watershed = {
        "type": "MultiPolygon",
        "coordinates": [
            [[[0, 0], [2, 0], [2, 2], [0, 2], [0, 0]]],
            [[[10, 10], [11, 10], [11, 11], [10, 11], [10, 10]]],
        ],
    }
    grids = [
        {"grid_id": "A", "geometry": poly([(0, 0), (2, 0), (2, 2), (0, 2), (0, 0)])},
        {"grid_id": "X", "geometry": poly(
            [(10, 10), (11, 10), (11, 11), (10, 11), (10, 10)]
        )},
    ]
    r = client.post("/publish", json={
        "expected_version": None, "watershed": watershed, "grids": grids,
    })
    assert r.status_code == 200, r.text
    r = client.post("/summary", json={
        "k": 2,
        "points": [
            pt("sec-alpha-1", 0.5, 0.5), pt("sec-alpha-2", 1.5, 1.5),  # A 内，公布
            pt("sec-beta-1", 10.5, 10.5),                              # X 内，不公布
        ],
    })
    assert r.status_code == 200, r.text
    body = r.json()
    # 顶层与区域的字段白名单
    assert set(body) == {"version", "is_current", "k", "regions", "unpublished_total"}
    assert [reg["grid_ids"] for reg in body["regions"]] == [["A"]]
    for reg in body["regions"]:
        assert set(reg) == {"grid_ids", "geometry", "point_count"}
    assert body["unpublished_total"] == 1
    # 未发布格 X 不出现在任何区域，也没有它的单格计数
    assert all("X" not in reg["grid_ids"] for reg in body["regions"])
    # 点号不泄露
    for secret in ("sec-alpha-1", "sec-alpha-2", "sec-beta-1"):
        assert secret not in r.text


def test_summary_in_openapi_and_docs(client):
    """/docs 可提交汇总：OpenAPI 中暴露 /summary 且文档页可用。"""
    r = client.get("/openapi.json")
    assert r.status_code == 200
    assert "/summary" in r.json()["paths"]
    assert client.get("/docs").status_code == 200
