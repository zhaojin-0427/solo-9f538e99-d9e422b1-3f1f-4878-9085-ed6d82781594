# 流域防汛网格服务

基于 **FastAPI + Shapely + SQLite** 的防汛网格整版发布与观测点落格查询 API。

## 业务规则

### 发布（`POST /publish`）

- 提交一个流域多边形和 **2～20 个**带唯一编号的网格 GeoJSON（Polygon 或 MultiPolygon）。
- 网格之间**内部不得重叠**（共边、共点的相切允许）。
- 网格合并后须**恰好覆盖**流域：既不能有缺口（`gap`），也不能越界（`excess`）。
- 任何重叠 / 缺口 / 越界都会在响应 `issues` 中给出问题位置（包围盒、代表点、面积），**整版拒绝**，当前版不变。
- 请求携带提交方**所见版号** `expected_version`（首次发布传 `null` 或 `0`）。两个并发改版只有一个能按该版号发布，另一个收到 `409 VERSION_CONFLICT`。
- 发布成功后版本号递增；旧版网格整版保留，永不改写。

### 查询（`POST /query`）

- 一次至多 **100 个**观测点；一次查询固定使用**同一版本**（`version` 不传 = 当前版）。
- 判定结果：
  - `inside`：点在某网格内部，返回唯一 `grid_id`；
  - `boundary`：点落在网格 / 流域边界上（含角点、洞边），列出所有相接网格，`grid_id` 为 `null`，标记为**待判**；
  - `outside`：流域外，网格列表为空。
- 指定的版本不存在返回 `404 VERSION_NOT_FOUND`；尚无任何已发布版本时查当前版返回 `404 NO_PUBLISHED_VERSION`。
- 旧版数据持久化在 SQLite 中，**进程 / 容器重启后仍可查询**。

### 脱敏汇总（`POST /summary`）

险情点位对外共享时，只公布满足最小事件数的区域汇总，不披露单点明细。

- 输入：已发布网格**版号**（`version` 不传或传 `null` = 固定当前版）、**1～30 个**险情点（`point_id` 必填且唯一、`lon/lat` 必须为有限数值）、匿名阈值 **k（2～5）**。
- **仅支持含 2～8 格的版本**；其他版本返回 `422 UNSUPPORTED_GRID_COUNT`。
- **严格落在格内**的点才参与汇总；落在边界上或流域外的点一律计入未发布总数。
- 发布区由**共享非零长度边**连通的 **1～3 格**组成（仅共点不算连通），各区互不重叠且每区点数 **≥ k**。
- 发布区规划按字典序取最优：**公布点数最多 → 发布区总面积最小 → 各区网格编号序列字典序最小**（保证结果唯一确定、可复现）。
- 响应：每个发布区的格号列表、合并后的 GeoJSON `geometry` 与点数，以及**未发布总数** `unpublished_total`（= 边界点 + 域外点 + 未发布格内点数）。
- **脱敏约束**：不返回点号、点坐标，也不返回未发布格的单格计数；无合格区时 `regions` 为空，只返回未发布总数。
- **快照一致性**：计算与返回使用同一版快照；网格改版不会改变指定旧版的结果，省略版号时固定为请求开始时的当前版。

## 快速启动（Docker Compose）

```bash
docker compose up --build
```

- 服务地址：http://localhost:8000
- 交互式文档（可直接提交和查询）：http://localhost:8000/docs
- SQLite 数据保存在命名卷 `watershed-data`（容器内 `/data/watershed.db`），重建容器不丢数据。

## 本地运行（无 Docker）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
DATABASE_PATH=data/watershed.db .venv/bin/uvicorn app.main:app --reload
```

## 请求示例

发布第一版：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v1.json
```

携带所见版号发布第二版（并发时只有一个成功，另一个返回 409）：

```bash
curl -X POST http://localhost:8000/publish \
  -H 'Content-Type: application/json' \
  -d @examples/publish_v2.json
```

批量查询（可传 `"version": 1` 固定查旧版，不传则查当前版）：

```bash
curl -X POST http://localhost:8000/query \
  -H 'Content-Type: application/json' \
  -d @examples/query.json
```

险情点脱敏汇总（可传 `"version": 1` 固定按旧版汇总，不传则固定当前版）：

```bash
curl -X POST http://localhost:8000/summary \
  -H 'Content-Type: application/json' \
  -d @examples/summary.json
```

响应示例（基于 `examples/publish_v1.json` 的 L/R 两格：L 内 3 点、R 内 1 点、边界 1 点、域外 1 点，k=2）：

```json
{
  "version": 1,
  "is_current": true,
  "k": 2,
  "regions": [
    {
      "grid_ids": ["L", "R"],
      "geometry": {"type": "Polygon", "coordinates": [[[0, 0], [4, 0], [4, 4], [0, 4], [0, 0]]]},
      "point_count": 4
    }
  ],
  "unpublished_total": 2
}
```

错误响应（重叠 / 缺口，HTTP 422）示例：

```json
{
  "error": "COVERAGE_INVALID",
  "detail": "网格存在重叠或未恰好覆盖流域，整版拒绝（问题位置见 issues）",
  "issues": [
    {
      "type": "overlap",
      "location": {"minx": 1.0, "miny": 0.0, "maxx": 2.0, "maxy": 4.0},
      "representative_point": [1.5, 2.0],
      "area": 4.0,
      "message": "网格 L 与 X 内部重叠，重叠面积 4"
    }
  ]
}
```

## 其他端点

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查与当前版号 |
| GET | `/versions` | 全部已发布版本清单 |
| POST | `/publish` | 整版发布（乐观锁） |
| POST | `/query` | 观测点批量落格 |
| POST | `/summary` | 险情点 k-匿名脱敏汇总 |
| GET | `/docs` | Swagger UI，可在线提交 / 查询 / 汇总 |

## 设计说明

- **乐观锁**：`meta.current_version` 记录当前版。发布在单条 `BEGIN IMMEDIATE` 事务内比对 `expected_version` 与当前版，不一致即回滚并返回 409；应用层另用写锁串行化同进程并发，SQLite `busy_timeout` 处理跨进程锁等待。
- **持久化**：几何以 WKB 存入 `versions` / `grids` 表，WAL 模式；每个版本独立行组，发布新版本不修改旧版任何行。
- **容差**：面积比较使用 `AREA_TOL_REL × 流域面积`（默认 `1e-9`）吸收浮点噪音；点查询对距边界极近（`POINT_EPS_REL`，默认 `1e-9` × 外接矩形对角线）的点兜底判为待判，避免浮点抖动把边界点误判为域外。
- **脱敏汇总规划**：网格数 ≤ 8，候选发布区（1~3 格、共享边连通、点数 ≥ k）至多为 C(8,1)+C(8,2)+C(8,3)=92 个，深度优先枚举全部互不重叠的候选组合，按（公布点数 ↓、总面积 ↑、编号序列字典序 ↑）取最优；共享边判定与面积比较均使用 `1e-9` 级相对容差吸收浮点噪音。汇总全程使用请求开始时一次性加载的版本快照，改版不影响本次与指定旧版的结果。
- 坐标系按平面坐标处理（经纬度直接作为 x/y），面积单位与输入坐标单位一致；如需米制面积请在入库前投影。

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：首版发布、版号乐观锁冲突、并发发布仅一个成功、网格数上下限、编号重复、重叠 / 缺口 / 越界定位、无效几何拒绝、失败发布不改当前版、内部 / 边界 / 角点 / 洞边 / 域外判定、100 点上限、旧版查询、版本不存在、重启后旧版仍可查；脱敏汇总覆盖：合格区合并与未发布总数、点数 / 面积 / 编号序列三级择优、共点不连通、3 格上限、无合格区只回总数、指定旧版不受改版影响、超 8 格版本拒绝、点编号唯一与坐标有限校验、30 点上限、响应不含点号 / 坐标 / 未发布格计数、OpenAPI 与 /docs 可用。
