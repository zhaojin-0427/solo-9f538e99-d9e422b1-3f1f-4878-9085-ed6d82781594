"""脱敏汇总：k-匿名发布区规划。

在某一版网格快照上，把严格落格的险情点按网格聚合计数，再从网格组合中
选出若干发布区对外公布：

- 每个发布区由 1~3 格组成，格与格之间经**共享非零长度边**连通（仅共点不算）；
- 发布区互不重叠，且每区公布的点数不少于阈值 k；
- 优化目标按字典序：公布点数最多 → 发布区总面积最小 → 各区网格编号
  序列字典序最小（保证结果唯一确定、可复现）。

本模块不导入 Shapely：几何对象只需提供 ``area`` 属性与 ``intersection``
方法（鸭子类型），因此可脱离几何库独立测试。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import combinations
from typing import Sequence

# 脱敏汇总仅支持 2~8 格的版本
MIN_SUMMARY_GRIDS = 2
MAX_SUMMARY_GRIDS = 8
# 每个发布区至多包含的网格数
MAX_REGION_GRIDS = 3
# 面积比较的相对容差，吸收不同求和顺序带来的浮点尾差
AREA_CMP_REL_TOL = 1e-9


@dataclass(frozen=True)
class RegionCandidate:
    """一个可独立成区的网格组合。"""

    mask: int  # 网格位掩码，用于重叠判断
    grid_ids: tuple[str, ...]  # 区内网格编号，已按字典序排序
    points: int  # 区内落格点数
    area: float  # 区内网格面积和


@dataclass(frozen=True)
class Plan:
    """一套发布方案：若干互不重叠的发布区。"""

    points: int  # 公布点总数
    area: float  # 发布区总面积
    regions: tuple[tuple[str, ...], ...]  # 各区的网格编号元组

    @property
    def sort_key(self) -> tuple[tuple[str, ...], ...]:
        """各区网格编号序列（排序后），用于同分择一。"""
        return tuple(sorted(self.regions))


def _better(a: Plan, b: Plan | None) -> bool:
    """a 是否优于 b：点数多者优先，再面积小者优先，再编号序列小者优先。"""
    if b is None:
        return True
    if a.points != b.points:
        return a.points > b.points
    tol = AREA_CMP_REL_TOL * max(1.0, a.area, b.area)
    if abs(a.area - b.area) > tol:
        return a.area < b.area
    return a.sort_key < b.sort_key


def _build_adjacency(
    grids: Sequence[tuple[str, object]], edge_eps: float
) -> list[list[bool]]:
    """共享非零长度边才算相邻：交集长度 > edge_eps（共点接触长度为零，不算）。"""
    n = len(grids)
    adj = [[False] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            shared = grids[i][1].intersection(grids[j][1]).length
            if shared > edge_eps:
                adj[i][j] = adj[j][i] = True
    return adj


def _connected(nodes: tuple[int, ...], adj: list[list[bool]]) -> bool:
    """判断网格下标集合在邻接关系下是否连通（BFS）。"""
    members = set(nodes)
    seen = {nodes[0]}
    stack = [nodes[0]]
    while stack:
        i = stack.pop()
        for j in members - seen:
            if adj[i][j]:
                seen.add(j)
                stack.append(j)
    return seen == members


def build_candidates(
    grids: Sequence[tuple[str, object]],
    counts: Sequence[int],
    k: int,
    edge_eps: float,
) -> list[RegionCandidate]:
    """枚举全部合格发布区候选：1~3 格、连通、点数 >= k。"""
    n = len(grids)
    adj = _build_adjacency(grids, edge_eps)
    candidates: list[RegionCandidate] = []
    for size in range(1, min(MAX_REGION_GRIDS, n) + 1):
        for combo in combinations(range(n), size):
            if size > 1 and not _connected(combo, adj):
                continue
            points = sum(counts[i] for i in combo)
            if points < k:
                continue
            area = math.fsum(grids[i][1].area for i in combo)
            grid_ids = tuple(sorted(grids[i][0] for i in combo))
            mask = 0
            for i in combo:
                mask |= 1 << i
            candidates.append(RegionCandidate(mask, grid_ids, points, area))
    # 固定枚举顺序，保证规划过程完全确定
    candidates.sort(key=lambda c: c.grid_ids)
    return candidates


def plan_regions(
    grids: Sequence[tuple[str, object]],
    counts: Sequence[int],
    k: int,
    edge_eps: float,
) -> Plan:
    """从候选发布区中选出互不重叠的一套，按字典序目标取最优。

    网格数 <= 8，候选数 <= C(8,1)+C(8,2)+C(8,3)=92，直接深度优先枚举
    所有互不重叠的候选组合并保留最优，结果精确且确定。
    """
    candidates = build_candidates(grids, counts, k, edge_eps)
    best = Plan(0, 0.0, ())

    def dfs(start: int, used: int, plan: Plan) -> None:
        nonlocal best
        if _better(plan, best):
            best = plan
        for idx in range(start, len(candidates)):
            cand = candidates[idx]
            if cand.mask & used:
                continue
            dfs(
                idx + 1,
                used | cand.mask,
                Plan(
                    plan.points + cand.points,
                    plan.area + cand.area,
                    plan.regions + (cand.grid_ids,),
                ),
            )

    dfs(0, 0, Plan(0, 0.0, ()))
    return best
