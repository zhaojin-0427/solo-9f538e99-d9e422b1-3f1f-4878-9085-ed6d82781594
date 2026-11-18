"""FastAPI 入口：流域网格发布、观测点批量查询与险情点脱敏汇总。"""
from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from shapely.geometry import mapping
from shapely.ops import unary_union

from .config import settings
from .db import Database, VersionConflict, VersionNotFound
from .geometry import GeometryError, classify_point, parse_surface, validate_submission
from .schemas import (
    PointResult,
    PublishRequest,
    PublishResponse,
    QueryRequest,
    QueryResponse,
    SummaryRegion,
    SummaryRequest,
    SummaryResponse,
    VersionInfo,
)
from .summary import MAX_SUMMARY_GRIDS, MIN_SUMMARY_GRIDS, plan_regions

app = FastAPI(
    title="流域防汛网格服务",
    description=(
        "提交流域多边形与整版网格（互不重叠且恰好覆盖流域），按所见版号乐观锁发布；"
        "观测点按固定版号批量落格，旧版持久保留可查；"
        "险情点按 k-匿名规则汇总为发布区后对外共享，不披露单点与未发布格明细。"
    ),
    version="1.1.0",
)

db = Database(settings.database_path)


def _error(status: int, error: str, detail: str, issues: list | None = None) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error, "detail": detail, "issues": issues},
    )


@app.exception_handler(GeometryError)
async def geometry_error_handler(_: Request, exc: GeometryError) -> JSONResponse:
    return _error(422, "INVALID_GEOMETRY", str(exc))


@app.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["运维"])
async def health() -> dict:
    return {"status": "ok", "current_version": db.current_version()}


@app.get("/versions", response_model=list[VersionInfo], tags=["版本"])
async def versions() -> list[dict]:
    return db.list_versions()


@app.post(
    "/publish",
    response_model=PublishResponse,
    tags=["发布"],
    responses={
        409: {"model": dict, "description": "版号冲突，整版拒绝，当前版不变"},
        422: {"model": dict, "description": "几何非法 / 网格重叠或未恰好覆盖流域，整版拒绝"},
    },
)
async def publish(req: PublishRequest) -> JSONResponse | PublishResponse:
    # 1) 解析流域
    watershed = parse_surface(req.watershed, "流域多边形")

    # 2) 解析网格并检查编号唯一
    grids: list[tuple[str, object]] = []
    seen_ids: set[str] = set()
    duplicates: list[str] = []
    for grid in req.grids:
        if grid.grid_id in seen_ids and grid.grid_id not in duplicates:
            duplicates.append(grid.grid_id)
        seen_ids.add(grid.grid_id)
        geom = parse_surface(grid.geometry, f"网格 {grid.grid_id}")
        grids.append((grid.grid_id, geom))
    if duplicates:
        return _error(
            422,
            "DUPLICATE_GRID_ID",
            f"网格编号必须唯一，重复编号：{', '.join(duplicates)}",
        )

    # 3) 几何整版校验：重叠 / 缺口 / 越界，任何问题整版拒绝
    issues = validate_submission(watershed, grids, settings.area_tol_rel)
    if issues:
        return _error(
            422,
            "COVERAGE_INVALID",
            "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
            issues,
        )

    # 4) 乐观锁发布（首次发布 expected_version 传 null 或 0）
    expected = None if req.expected_version in (None, 0) else req.expected_version
    try:
        new_version = db.publish(expected, watershed, grids)
    except VersionConflict as exc:
        return _error(409, "VERSION_CONFLICT", str(exc))

    info = next(v for v in db.list_versions() if v["version"] == new_version)
    return PublishResponse(
        version=new_version,
        expected_version=expected,
        published_at=info["published_at"],
        grid_count=info["grid_count"],
    )


@app.post(
    "/query",
    response_model=QueryResponse,
    tags=["查询"],
    responses={404: {"model": dict, "description": "指定版本不存在或尚无已发布版本"}},
)
async def query(req: QueryRequest) -> JSONResponse | QueryResponse:
    current = db.current_version()
    if req.version is None:
        if current is None:
            return _error(404, "NO_PUBLISHED_VERSION", "尚无已发布版本，请先调用 /publish")
        version = current
    else:
        version = req.version
        if not any(v["version"] == version for v in db.list_versions()):
            return _error(404, "VERSION_NOT_FOUND", f"版本 {version} 不存在")

    watershed, grids = db.load_snapshot(version)
    diag = max(watershed.bounds[2] - watershed.bounds[0],
               watershed.bounds[3] - watershed.bounds[1])
    eps = diag * settings.point_eps_rel

    results: list[PointResult] = []
    for p in req.points:
        verdict = classify_point(p.lon, p.lat, watershed, grids, eps)
        results.append(
            PointResult(
                point_id=p.point_id,
                lon=p.lon,
                lat=p.lat,
                status=verdict["status"],
                grid_id=verdict["grid_id"],
                grid_ids=verdict["grid_ids"],
            )
        )

    return QueryResponse(version=version, is_current=(version == current), results=results)


@app.post(
    "/summary",
    response_model=SummaryResponse,
    tags=["脱敏汇总"],
    responses={
        404: {"model": dict, "description": "指定版本不存在或尚无已发布版本"},
        422: {"model": dict, "description": "点编号重复 / 坐标非法 / 版本网格数不在 2~8"},
    },
)
async def summarize(req: SummaryRequest) -> JSONResponse | SummaryResponse:
    # 1) 固定版号：省略时取请求开始时的当前版，全程不再变化
    current = db.current_version()
    if req.version is None:
        if current is None:
            return _error(404, "NO_PUBLISHED_VERSION", "尚无已发布版本，请先调用 /publish")
        version = current
    else:
        version = req.version
        if not any(v["version"] == version for v in db.list_versions()):
            return _error(404, "VERSION_NOT_FOUND", f"版本 {version} 不存在")

    # 2) 险情点编号唯一（坐标有限性由请求模型校验）
    seen: set[str] = set()
    duplicates: list[str] = []
    for p in req.points:
        if p.point_id in seen and p.point_id not in duplicates:
            duplicates.append(p.point_id)
        seen.add(p.point_id)
    if duplicates:
        return _error(
            422,
            "DUPLICATE_POINT_ID",
            f"险情点编号必须唯一，重复编号：{', '.join(duplicates)}",
        )

    # 3) 一次性加载该版快照：此后计数、规划、合并均基于这份内存数据，
    #    网格改版不会改变本次（及指定旧版的）结果
    watershed, grids = db.load_snapshot(version)
    if not MIN_SUMMARY_GRIDS <= len(grids) <= MAX_SUMMARY_GRIDS:
        return _error(
            422,
            "UNSUPPORTED_GRID_COUNT",
            f"版本 {version} 含 {len(grids)} 个网格，脱敏汇总仅支持 "
            f"{MIN_SUMMARY_GRIDS}~{MAX_SUMMARY_GRIDS} 格的版本",
        )

    # 4) 落格计数：严格格内点参与汇总；边界与域外点一律计入未发布总数
    diag = max(watershed.bounds[2] - watershed.bounds[0],
               watershed.bounds[3] - watershed.bounds[1])
    eps = diag * settings.point_eps_rel
    index = {grid_id: i for i, (grid_id, _) in enumerate(grids)}
    counts = [0] * len(grids)
    for p in req.points:
        verdict = classify_point(p.lon, p.lat, watershed, grids, eps)
        if verdict["status"] == "inside":
            counts[index[verdict["grid_id"]]] += 1

    # 5) 规划发布区并合并几何；无合格区时 regions 为空，只返回未发布总数
    plan = plan_regions(grids, counts, req.k, eps)
    geom_by_id = dict(grids)
    regions: list[SummaryRegion] = []
    for grid_ids in sorted(plan.regions):
        merged = unary_union([geom_by_id[g] for g in grid_ids])
        regions.append(
            SummaryRegion(
                grid_ids=list(grid_ids),
                geometry=mapping(merged),
                point_count=sum(counts[index[g]] for g in grid_ids),
            )
        )
    published = sum(r.point_count for r in regions)
    return SummaryResponse(
        version=version,
        is_current=(version == current),
        k=req.k,
        regions=regions,
        unpublished_total=len(req.points) - published,
    )
