"""请求 / 响应 Pydantic 模型。"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 状态码：inside 内部点 / boundary 边界待判 / outside 流域外
PointStatus = Literal["inside", "boundary", "outside"]


class GridIn(BaseModel):
    grid_id: str = Field(..., min_length=1, max_length=128, description="网格唯一编号")
    geometry: dict[str, Any] = Field(..., description="Polygon 或 MultiPolygon 的 GeoJSON geometry")


class PublishRequest(BaseModel):
    expected_version: int | None = Field(
        None,
        ge=0,
        description="提交方所见的当前版号；首次发布传 null 或 0，之后传当前版号",
    )
    watershed: dict[str, Any] = Field(..., description="流域多边形 GeoJSON geometry")
    grids: list[GridIn] = Field(..., min_length=2, max_length=20, description="2~20 个带唯一编号的网格")


class ValidationIssue(BaseModel):
    type: Literal["overlap", "gap", "excess"]
    location: dict[str, float]
    representative_point: list[float]
    area: float
    message: str


class PublishResponse(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int
    status: Literal["published"] = "published"


class ErrorResponse(BaseModel):
    error: str
    detail: str
    issues: list[ValidationIssue] | None = None


class QueryPoint(BaseModel):
    point_id: str | None = Field(None, max_length=128, description="观测点编号，可选，便于结果对应")
    lon: float
    lat: float


class QueryRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="固定查询版号；不传或传 null 表示当前版，一次查询内所有点同版"
    )
    points: list[QueryPoint] = Field(..., min_length=1, max_length=100, description="至多 100 个观测点")


class PointResult(BaseModel):
    point_id: str | None
    lon: float
    lat: float
    status: PointStatus
    grid_id: str | None = Field(None, description="内部点返回唯一网格编号；其余为 null")
    grid_ids: list[str] = Field(
        default_factory=list, description="边界点列出所有相接网格（待判）；域外为空"
    )


class QueryResponse(BaseModel):
    version: int = Field(..., description="本次查询实际使用的版号")
    is_current: bool
    results: list[PointResult]


class VersionInfo(BaseModel):
    version: int
    expected_version: int | None
    published_at: str
    grid_count: int


# ---------- 脱敏汇总 ----------


class SummaryPoint(BaseModel):
    point_id: str = Field(
        ..., min_length=1, max_length=128, description="险情点编号，必填且同次请求内唯一"
    )
    lon: float = Field(..., allow_inf_nan=False, description="经度，必须为有限数值")
    lat: float = Field(..., allow_inf_nan=False, description="纬度，必须为有限数值")


class SummaryRequest(BaseModel):
    version: int | None = Field(
        None, ge=1, description="已发布网格版号；不传或传 null 表示固定当前版"
    )
    k: int = Field(..., ge=2, le=5, description="匿名阈值：每个发布区至少包含 k 个险情点")
    points: list[SummaryPoint] = Field(
        ..., min_length=1, max_length=30, description="1~30 个险情点，编号唯一且坐标有限"
    )


class SummaryRegion(BaseModel):
    grid_ids: list[str] = Field(..., description="发布区包含的网格编号（字典序）")
    geometry: dict[str, Any] = Field(..., description="发布区合并后的 GeoJSON geometry")
    point_count: int = Field(..., description="该发布区公布的险情点数（>= k）")


class SummaryResponse(BaseModel):
    version: int = Field(..., description="本次汇总实际使用的版号")
    is_current: bool = Field(..., description="本次使用的版号是否为当前版")
    k: int
    regions: list[SummaryRegion] = Field(
        ..., description="合格发布区列表；无合格区时为空数组"
    )
    unpublished_total: int = Field(
        ...,
        description="未公布险情点总数：边界点、域外点与未发布格内点数之和（不披露单格计数）",
    )
