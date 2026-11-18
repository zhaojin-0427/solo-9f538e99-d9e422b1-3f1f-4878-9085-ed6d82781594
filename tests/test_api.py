"""端到端测试：通过 FastAPI TestClient 覆盖关键业务情形。"""
from __future__ import annotations

import importlib
import os
import threading

import pytest
from fastapi.testclient import TestClient


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
        yield c, main, tmp_path


def poly(coords):
    return {"type": "Polygon", "coordinates": [coords]}


# 0,0 → 4,4 的流域，拆成左右两个 2×4 网格（共享边 x=2）
WATERSHED = poly([(0, 0), (4, 0), (4, 4), (0, 4), (0, 0)])
LEFT = poly([(0, 0), (2, 0), (2, 4), (0, 4), (0, 0)])
RIGHT = poly([(2, 0), (4, 0), (4, 4), (2, 4), (2, 0)])


def publish_body(expected=None, watershed=WATERSHED, grids=None):
    return {
        "expected_version": expected,
        "watershed": watershed,
        "grids": grids
        or [
            {"grid_id": "L", "geometry": LEFT},
            {"grid_id": "R", "geometry": RIGHT},
        ],
    }


# ---------- 发布主流程 ----------

def test_first_publish_creates_version_1(client):
    c, _, _ = client
    r = c.post("/publish", json=publish_body(expected=None))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1
    assert body["grid_count"] == 2
    assert c.get("/health").json()["current_version"] == 1


def test_first_publish_accepts_zero_as_null(client):
    c, _, _ = client
    r = c.post("/publish", json=publish_body(expected=0))
    assert r.status_code == 200
    assert r.json()["version"] == 1


def test_second_version_publish_and_optimistic_conflict(client):
    c, _, _ = client
    assert c.post("/publish", json=publish_body()).status_code == 200
    # 带正确所见版号 1 发布第 2 版
    r2 = c.post("/publish", json=publish_body(expected=1))
    assert r2.status_code == 200
    assert r2.json()["version"] == 2
    # 旧版号 1 再发布 → 409，当前版仍为 2
    r3 = c.post("/publish", json=publish_body(expected=1))
    assert r3.status_code == 409
    assert r3.json()["error"] == "VERSION_CONFLICT"
    assert c.get("/health").json()["current_version"] == 2


def test_concurrent_publish_only_one_wins(client):
    c, main, tmp_path = client
    assert c.post("/publish", json=publish_body()).status_code == 200
    outcomes: list[str] = []
    barrier = threading.Barrier(2)

    def worker():
        barrier.wait()
        # 两个线程都以所见版号 1 并发发布
        try:
            version = main.db.publish(1, *main.db.load_snapshot(1))
            outcomes.append(f"ok:{version}")
        except Exception as exc:  # 失败者必须收到版号冲突
            outcomes.append(f"conflict:{type(exc).__name__}")

    t1 = threading.Thread(target=worker)
    t2 = threading.Thread(target=worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == ["conflict:VersionConflict", "ok:2"]
    assert main.db.current_version() == 2


def test_concurrent_http_publish_only_one_wins(client):
    """经真实 HTTP 端点并发：一个 200、一个 409，且只产生一个新版本。"""
    c, main, _ = client
    assert c.post("/publish", json=publish_body()).status_code == 200
    statuses: list[int] = []
    barrier = threading.Barrier(2)

    def http_worker():
        barrier.wait()
        r = c.post("/publish", json=publish_body(expected=1))
        statuses.append(r.status_code)

    t1 = threading.Thread(target=http_worker)
    t2 = threading.Thread(target=http_worker)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(statuses) == [200, 409]
    assert main.db.current_version() == 2
    assert [v["version"] for v in main.db.list_versions()] == [1, 2]


# ---------- 校验拒绝（整版拒绝，当前版不变） ----------

def test_grid_count_limits(client):
    c, _, _ = client
    one = publish_body(grids=[{"grid_id": "L", "geometry": LEFT}])
    assert c.post("/publish", json=one).status_code == 422
    many = publish_body(
        grids=[{"grid_id": f"G{i}", "geometry": LEFT} for i in range(21)]
    )
    assert c.post("/publish", json=many).status_code == 422


def test_duplicate_grid_id_rejected(client):
    c, _, _ = client
    body = publish_body(grids=[
        {"grid_id": "X", "geometry": LEFT},
        {"grid_id": "X", "geometry": RIGHT},
    ])
    r = c.post("/publish", json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "DUPLICATE_GRID_ID"
    assert c.get("/health").json()["current_version"] is None


def test_overlap_rejected_with_location(client):
    c, _, _ = client
    overlap = poly([(1, 0), (3, 0), (3, 4), (1, 4), (1, 0)])
    body = publish_body(grids=[
        {"grid_id": "L", "geometry": LEFT},
        {"grid_id": "X", "geometry": overlap},
    ])
    r = c.post("/publish", json=body)
    assert r.status_code == 422
    payload = r.json()
    assert payload["error"] == "COVERAGE_INVALID"
    assert any(i["type"] == "overlap" for i in payload["issues"])
    issue = next(i for i in payload["issues"] if i["type"] == "overlap")
    assert issue["location"]["minx"] == pytest.approx(1.0)
    assert issue["area"] == pytest.approx(4.0)


def test_gap_rejected_with_location(client):
    c, _, _ = client
    # 左网格只覆盖 x∈[0,1]，留出 x∈[1,2] 的缺口
    narrow_left = poly([(0, 0), (1, 0), (1, 4), (0, 4), (0, 0)])
    body = publish_body(grids=[
        {"grid_id": "L", "geometry": narrow_left},
        {"grid_id": "R", "geometry": RIGHT},
    ])
    r = c.post("/publish", json=body)
    assert r.status_code == 422
    types = {i["type"] for i in r.json()["issues"]}
    assert "gap" in types


def test_excess_rejected(client):
    c, _, _ = client
    too_big = poly([(2, 0), (5, 0), (5, 4), (2, 4), (2, 0)])
    body = publish_body(grids=[
        {"grid_id": "L", "geometry": LEFT},
        {"grid_id": "R", "geometry": too_big},
    ])
    r = c.post("/publish", json=body)
    assert r.status_code == 422
    assert any(i["type"] == "excess" for i in r.json()["issues"])


def test_invalid_geojson_rejected(client):
    c, _, _ = client
    body = publish_body(watershed={"type": "LineString", "coordinates": [[0, 0], [1, 1]]})
    r = c.post("/publish", json=body)
    assert r.status_code == 422
    assert r.json()["error"] == "INVALID_GEOMETRY"


def test_failed_publish_does_not_change_current(client):
    c, _, _ = client
    assert c.post("/publish", json=publish_body()).status_code == 200
    bad = publish_body(expected=1, grids=[
        {"grid_id": "L", "geometry": LEFT},
        {"grid_id": "R", "geometry": LEFT},  # 不覆盖流域
    ])
    r = c.post("/publish", json=bad)
    assert r.status_code == 422
    assert c.get("/health").json()["current_version"] == 1
    assert len(c.get("/versions").json()) == 1


# ---------- 查询 ----------

def test_query_inside_boundary_outside(client):
    c, _, _ = client
    assert c.post("/publish", json=publish_body()).status_code == 200
    r = c.post("/query", json={"points": [
        {"point_id": "in-l", "lon": 1.0, "lat": 2.0},
        {"point_id": "in-r", "lon": 3.5, "lat": 1.0},
        {"point_id": "edge", "lon": 2.0, "lat": 2.0},
        {"point_id": "corner", "lon": 0.0, "lat": 0.0},
        {"point_id": "out", "lon": 9.0, "lat": 9.0},
    ]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == 1 and body["is_current"] is True
    by_id = {x["point_id"]: x for x in body["results"]}
    assert by_id["in-l"]["status"] == "inside" and by_id["in-l"]["grid_id"] == "L"
    assert by_id["in-r"]["grid_id"] == "R"
    edge = by_id["edge"]
    assert edge["status"] == "boundary" and edge["grid_id"] is None
    assert sorted(edge["grid_ids"]) == ["L", "R"]
    assert by_id["corner"]["status"] == "boundary"
    assert by_id["out"]["status"] == "outside" and by_id["out"]["grid_ids"] == []


def test_query_limit_100(client):
    c, _, _ = client
    c.post("/publish", json=publish_body())
    ok = {"points": [{"lon": 1, "lat": 1} for _ in range(100)]}
    assert c.post("/query", json=ok).status_code == 200
    too_many = {"points": [{"lon": 1, "lat": 1} for _ in range(101)]}
    assert c.post("/query", json=too_many).status_code == 422


def test_query_old_version_and_version_not_found(client):
    c, _, _ = client
    c.post("/publish", json=publish_body())
    # 第 2 版：上下两个网格，编号完全不同，验证旧版归属不被改写
    top = poly([(0, 2), (4, 2), (4, 4), (0, 4), (0, 2)])
    bottom = poly([(0, 0), (4, 0), (4, 2), (0, 2), (0, 0)])
    r = c.post("/publish", json=publish_body(expected=1, grids=[
        {"grid_id": "B", "geometry": bottom},
        {"grid_id": "T", "geometry": top},
    ]))
    assert r.status_code == 200
    # 当前版查询点 (1,1) 落在 B；旧版 1 同点落在 L
    cur = c.post("/query", json={"points": [{"lon": 1, "lat": 1}]}).json()
    old = c.post("/query", json={"version": 1, "points": [{"lon": 1, "lat": 1}]}).json()
    assert cur["results"][0]["grid_id"] == "B" and cur["is_current"] is True
    assert old["results"][0]["grid_id"] == "L" and old["is_current"] is False
    assert c.post("/query", json={"version": 9, "points": []}).status_code == 422
    r404 = c.post("/query", json={"version": 9, "points": [{"lon": 1, "lat": 1}]})
    assert r404.status_code == 404
    assert r404.json()["error"] == "VERSION_NOT_FOUND"


def test_query_before_any_publish_is_404(client):
    c, _, _ = client
    r = c.post("/query", json={"points": [{"lon": 1, "lat": 1}]})
    assert r.status_code == 404
    assert r.json()["error"] == "NO_PUBLISHED_VERSION"


# ---------- 持久化：模拟重启 ----------

def test_old_versions_survive_restart(client):
    c, main, tmp_path = client
    c.post("/publish", json=publish_body())
    c.post("/publish", json=publish_body(expected=1))
    db_path = main.db.path

    # 丢弃连接并重新初始化 Database 对象，等价于进程重启
    new_db = type(main.db)(db_path)
    assert new_db.current_version() == 2
    watershed, grids = new_db.load_snapshot(1)
    assert sorted(gid for gid, _ in grids) == ["L", "R"]
    assert watershed.area == 16.0
    versions = new_db.list_versions()
    assert [v["version"] for v in versions] == [1, 2]
