# GeoTerritory

地理归属后端（Go + Gin + GORM + MySQL）。在本地实现闭合多边形区域管理、
点在多边形内判定、优先级稳定分配、区域不可变版本发布、区域改版触发的持久化
重算任务，以及组织隔离的包围盒 / 最近 N 点查询。**不包含** CRM 流程、地图
界面或任何外部地理服务。

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后：

- API： http://127.0.0.1:8080
- 健康检查： `GET /healthz`
- 预置两个相互隔离的组织（仅用于演示）：
  - `org-a`，API key `demo-key-a`
  - `org-b`，API key `demo-key-b`

端到端演示（需要 `curl` 和 `jq`）：

```bash
./examples/demo.sh
```

## 本地运行（已有 MySQL 时）

```bash
export MYSQL_DSN='root:root@tcp(127.0.0.1:3306)/geoterritory?charset=utf8mb4&parseTime=true&loc=UTC&multiStatements=true'
go run ./cmd/server
```

应用启动时自动执行 `migrations/` 下的版本化 SQL 并幂等播种组织。
`multiStatements=true` 是迁移执行器的必需参数。

## 几何规则

- 区域为**闭合简单多边形**，提交顶点环时**不要重复首尾点**；至少 3 个
  互不相同的顶点。
- 校验经纬度范围（纬度 ±90、经度 ±180）、有限数值、**自交**（含非邻边
  接触）、顶点落在非邻接边上的退化刺，以及零面积共线环。
- **边界点规则**：点在任意一条边（含顶点、含首尾闭合边）上即判定为区域
  内，响应中以 `on_boundary: true` 标出。
- **跨 180° 经线**：任何经度跨度 ≥ 180° 的边直接拒绝（HTTP 400）；
  `min_lng > max_lng` 的跨日界线包围盒同样拒绝。系统不会把这种输入静默
  解释成另一个小多边形。需要表示日界线附近区域时，请拆成不跨线的多个区域。
- 多个区域重叠时按 `priority`（1 最高，5 最低）取最小；优先级相同按区域
  ID 升序，结果确定稳定。区域外点标记为未分配（`region_id = 0`）。

## 版本与重算模型

- 每次发布区域都会生成一个**不可变目录版本**（catalog version）。区域
  多边形本身也以 `region_versions` 不可变快照保存。
- `current_version` 是查询和归属所依据的版本；`published_version` 在发布
  时立即前进。二者不同期间存在一个持久化的**重算任务**。
- 后台 worker 分批为新版本填充归属，完成后在组织级命名锁内做最后的遗漏
  清扫，并用 CAS 把 `current_version` **原子切换**到新版本。
- 归属结果按目录版本分别存储：**旧版本查询永远一致**。
- 重算期间新增/改坐标的点会同时写入新旧两个在线版本，且 worker 用
  `INSERT IGNORE` 填充、写点路径用 `ON DUPLICATE KEY UPDATE` 持有最新坐标；
  迟到/被接管的旧任务心跳失效即停止，最终翻转带 `from_version` CAS，旧
  任务**不可能覆盖新结果**。
- 崩溃恢复：任务状态为 `PENDING/RUNNING`，心跳超时（默认 30s）后其它
  worker 可接管；批次插入幂等，可安全重放。每组织至多一个活动任务（由
  生成列 + 唯一索引在数据库层保证）。

## 点位导入

`POST /v1/points/batch`，单次最多 **1000** 条，按组织内 `external_id`
幂等：

- 同 ID 且坐标相同：幂等成功，版本号不变；
- 同 ID 坐标变化：**必须**提供 `expected_version`（当前 `version`），
  否则 `VERSION_CONFLICT`；版本不匹配同样冲突并返回当前版本号；
- 批处理返回逐行结果，非法行不影响合法行落库；
- 整批在组织锁 + 单事务内执行，并发修改同一点只有一个赢家，更新不会丢失。

## 查询

- `GET /v1/points/bbox`：组织限定的包围盒查询（不支持跨日界线）。
- `GET /v1/points/nearest`：最近 N 点，`n` 最大 **50**；距离用
  **Haversine**（米，地球半径 6,371,000 m）在 SQL 中计算，距离相同按
  点 ID 升序打破平局。
- 两个查询都以 `org_id` 强制过滤并 JOIN 对应目录版本的归属：**无法返回
  未授权组织的点**。
- 所有归属查询支持 `?catalog_version=current|published|<int>`。

完整接口说明见 [`docs/API.md`](docs/API.md)，几何样例见
[`examples/`](examples/)。

## 测试

```bash
make test                 # 纯单元测试（几何、分配引擎）
make test-mysql-up        # 启动一次性 MySQL（:33061）
make test-integration     # 全部测试（含竞态检测 -race）
make test-mysql-down
```

集成测试覆盖：边界点与顶点、自交/退化/跨经线拒绝、区域重叠优先级与 ID
平局、逐行错误与部分成功、1000 条上限、同点并发更新不丢失、重算期间
新点不遗漏、崩溃恢复接管、迟到任务不能覆盖、旧版本读取一致、以及跨组织
权限隔离。

## 目录结构

```
cmd/server          入口：迁移、播种、HTTP、worker
geometry            纯本地几何：校验、点在多边形内、Haversine
internal/engine     目录视图与优先级稳定分配
internal/models     GORM 模型（SQL 迁移为 schema 唯一真实来源）
internal/store      DB、组织命名锁、发布、批导入、任务、查询
internal/worker     持久化重算后台循环（领取/心跳/接管/原子切换）
internal/api        Gin 路由与处理器
internal/integration 端到端测试（真实 MySQL）
migrations          版本化 SQL
examples            几何样例与 curl 演示
docs                API 文档
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HTTP_ADDR` | `:8080` | 监听地址 |
| `MYSQL_DSN` | 本机 root DSN | 必须含 `multiStatements=true` |
| `MIGRATION_DIR` | `migrations` | 迁移 SQL 目录 |
| `SEED_API_KEYS` | `org-a=demo-key-a,org-b=demo-key-b` | 播种组织 `name=key` 列表 |
| `WORKER_ENABLED` | `true` | 是否在本进程运行重算 worker |
| `WORKER_TICK_MS` | `500` | 轮询间隔 |
| `REASSIGN_BATCH_SIZE` | `500` | 单批处理点数 |
| `STALE_JOB_SECONDS` | `30` | 心跳多久无更新可被接管 |
| `ORG_LOCK_TIMEOUT_SECONDS` | `15` | 组织命名锁等待上限 |
