"""Shapely 几何处理：整版网格校验与观测点落格判定。"""
from __future__ import annotations

from typing import Any

from shapely.geometry import shape, Point
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union
from shapely.strtree import STRtree
from shapely.validation import explain_validity

# 缺口 / 重叠报告最多返回的局部数量，避免错误负载过大
MAX_ISSUE_PARTS = 20
SUPPORTED_GEOM_TYPES = {"Polygon", "MultiPolygon"}


class GeometryError(ValueError):
    """请求中的 GeoJSON 无法解析或不是合法的面几何。"""


def _bbox_dict(geom: BaseGeometry) -> dict[str, float]:
    minx, miny, maxx, maxy = geom.bounds
    return {"minx": minx, "miny": miny, "maxx": maxx, "maxy": maxy}


def _issue(kind: str, geom: BaseGeometry, message: str) -> dict[str, Any]:
    return {
        "type": kind,
        "location": _bbox_dict(geom),
        "representative_point": list(geom.representative_point().coords[0]),
        "area": geom.area,
        "message": message,
    }


def parse_surface(geojson: dict[str, Any], label: str) -> BaseGeometry:
    """把 GeoJSON geometry 解析为 Polygon/MultiPolygon，并做有效性检查。"""
    if not isinstance(geojson, dict):
        raise GeometryError(f"{label} 必须是 GeoJSON geometry 对象")
    geom_type = geojson.get("type")
    if geom_type not in SUPPORTED_GEOM_TYPES:
        raise GeometryError(
            f"{label} 的 type 必须是 Polygon 或 MultiPolygon，收到 {geom_type!r}"
        )
    try:
        geom = shape(geojson)
    except Exception as exc:  # shapely 对非法 GeoJSON 抛 GEOSException/ValueError
        raise GeometryError(f"{label} 不是合法的 GeoJSON：{exc}") from exc
    if not geom.is_valid:
        raise GeometryError(f"{label} 几何无效：{explain_validity(geom)}")
    if geom.is_empty or geom.area <= 0.0:
        raise GeometryError(f"{label} 为空或面积为零")
    return geom


def validate_submission(
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    area_tol_rel: float,
) -> list[dict[str, Any]]:
    """校验网格互不重叠且恰好覆盖流域，返回问题清单（空清单代表通过）。"""
    issues: list[dict[str, Any]] = []
    area_tol = max(watershed.area * area_tol_rel, 0.0)

    # 1) 两两检查内部重叠（相切不算重叠）
    for i in range(len(grids)):
        id_a, geom_a = grids[i]
        for j in range(i + 1, len(grids)):
            id_b, geom_b = grids[j]
            inter = geom_a.intersection(geom_b)
            if not inter.is_empty and inter.area > area_tol:
                issues.append(
                    _issue(
                        "overlap",
                        inter,
                        f"网格 {id_a} 与 {id_b} 内部重叠，重叠面积 {inter.area:g}",
                    )
                )

    # 2) 合并后与流域做对称差，区分缺口与越界
    union = unary_union([geom for _, geom in grids])
    sym_diff = union.symmetric_difference(watershed)
    gap = sym_diff.intersection(watershed)  # 流域内未被网格覆盖的部分
    excess = sym_diff.difference(watershed)  # 网格超出流域的部分

    if not gap.is_empty and gap.area > area_tol:
        parts = list(gap.geoms) if gap.geom_type == "MultiPolygon" else [gap]
        parts = [p for p in parts if p.area > area_tol]
        for part in parts[:MAX_ISSUE_PARTS]:
            issues.append(_issue("gap", part, "网格未覆盖流域，存在缺口"))
        if len(parts) > MAX_ISSUE_PARTS:
            issues.append(
                {
                    "type": "gap",
                    "location": _bbox_dict(gap),
                    "representative_point": list(
                        gap.representative_point().coords[0]
                    ),
                    "area": gap.area,
                    "message": f"缺口过多，共 {len(parts)} 处，仅报告前 {MAX_ISSUE_PARTS} 处",
                }
            )

    if not excess.is_empty and excess.area > area_tol:
        parts = (
            list(excess.geoms)
            if excess.geom_type == "MultiPolygon"
            else [excess]
        )
        parts = [p for p in parts if p.area > area_tol]
        for part in parts[:MAX_ISSUE_PARTS]:
            issues.append(_issue("excess", part, "网格超出流域边界"))
        if len(parts) > MAX_ISSUE_PARTS:
            issues.append(
                {
                    "type": "excess",
                    "location": _bbox_dict(excess),
                    "representative_point": list(
                        excess.representative_point().coords[0]
                    ),
                    "area": excess.area,
                    "message": f"越界块过多，共 {len(parts)} 处，仅报告前 {MAX_ISSUE_PARTS} 处",
                }
            )

    return issues


def _make_point(lon: Any, lat: Any) -> Point:
    try:
        point = Point(float(lon), float(lat))
    except (TypeError, ValueError) as exc:
        raise GeometryError(f"观测点坐标必须是数字，收到 {lon!r}, {lat!r}") from exc
    if not point.is_valid:
        raise GeometryError("观测点不是合法的点几何")
    return point


def classify_point(
    lon: Any,
    lat: Any,
    watershed: BaseGeometry,
    grids: list[tuple[str, BaseGeometry]],
    eps: float,
) -> dict[str, Any]:
    """判定单个观测点：内部点落唯一网格；边界点列出相接网格待判；域外返回域外。"""
    point = _make_point(lon, lat)

    # 先用空间索引找出外接框相交的候选网格，再精确判定
    tree = STRtree([geom for _, geom in grids])
    candidate_idx = list(tree.query(point))
    inside_ids: list[str] = []
    touching: list[str] = []
    for idx in candidate_idx:
        grid_id, geom = grids[int(idx)]
        # contains 不含边界；touches 表示交点只落在边界上
        if geom.contains(point):
            inside_ids.append(grid_id)
        elif geom.touches(point):
            touching.append(grid_id)

    # 已发布版本保证网格互不重叠；正常情况下内部点唯一，异常数据则转待判
    if len(inside_ids) == 1 and not touching:
        grid_id = inside_ids[0]
        return {"status": "inside", "grid_id": grid_id, "grid_ids": [grid_id]}
    if inside_ids or touching:
        pending = sorted(set(inside_ids) | set(touching))
        return {"status": "boundary", "grid_id": None, "grid_ids": pending}

    # 浮点兜底：点在流域边界极近邻内（且流域本身包含/相接该点时）按待判处理
    if watershed.dwithin(point, eps):
        near = sorted(
            grid_id
            for grid_id, geom in grids
            if geom.dwithin(point, eps)
        )
        if near:
            return {"status": "boundary", "grid_id": None, "grid_ids": near}

    return {"status": "outside", "grid_id": None, "grid_ids": []}
