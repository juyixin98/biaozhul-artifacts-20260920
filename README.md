# SignalBoard

连锁餐厅数字菜单屏的发布与同步 API。Go + Chi + sqlc + PostgreSQL，编译为静态二进制，
除 PostgreSQL 外不依赖其他服务。

## 快速开始

```bash
docker compose up -d --build     # app: http://localhost:8123, db: localhost:5434
./scripts/seed.sh                # 写入演示门店/菜品/临时价格/屏幕，并打印屏幕令牌
```

本地开发（需要 Go 1.22+ 与 PostgreSQL 16）：

```bash
export DATABASE_URL=postgres://signalboard:signalboard@localhost:5434/signalboard?sslmode=disable
export ADMIN_TOKEN=dev-admin-token
go run ./cmd/signalboard         # 启动时自动执行迁移
```

### 测试

```bash
# 集成测试需要 PostgreSQL；未设置 TEST_DATABASE_URL 时自动跳过
docker run -d --name signalboard-test-db -p 5433:5432 \
  -e POSTGRES_USER=signalboard -e POSTGRES_PASSWORD=signalboard \
  -e POSTGRES_DB=signalboard postgres:16-alpine
TEST_DATABASE_URL=postgres://signalboard:signalboard@localhost:5433/signalboard?sslmode=disable \
  go test ./...
```

测试覆盖：并发发布（8 并发仅 1 成功）、导入整批回滚与 500 上限、临时价格时间边界
（左闭右开、UTC 偏移换算、重叠拒绝、并发重叠仅 1 成功）、销售幂等（重复不计、
同 ID 不同内容 409、并发不重复计）、阈值售罄与跨日恢复、迟到事件归实际发生日、
ETag 失效（发布/售罄/临时价格到点）、屏幕令牌隔离、心跳离线判定。

### 静态二进制

```bash
CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o signalboard ./cmd/signalboard
```

无 CGO、无动态链接（`ldd` 显示 not a dynamic executable），IANA 时区库通过
`time/tzdata` 内嵌，镜像基于 `gcr.io/distroless/static-debian12`。

## 配置

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgres://signalboard:signalboard@localhost:5432/signalboard?sslmode=disable` | PostgreSQL 连接串 |
| `PORT` | `8080` | 监听端口 |
| `ADMIN_TOKEN` | `dev-admin-token` | 管理端 Bearer 令牌（生产环境务必修改） |

## 认证

- 管理接口：`Authorization: Bearer <ADMIN_TOKEN>`
- 屏幕接口：`Authorization: Bearer <屏幕令牌>`（创建屏幕时返回一次，服务端只存 SHA-256 哈希）。
  屏幕令牌绑定门店，只能读取所属门店的菜单与发送心跳。

## API 概览

错误统一为 `{"error":{"code","message"}}`。

### 门店与草稿（管理端）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/stores` | 创建门店 `{name, timezone}`（IANA 时区，非法时区 400） |
| GET | `/v1/stores` / `/v1/stores/{id}` | 列表 / 详情（含 `current_version`） |
| POST | `/v1/stores/{id}/items` | 新增草稿菜品（价格为整数分 `price_cents`） |
| GET | `/v1/stores/{id}/items` | 草稿列表 |
| PUT / DELETE | `/v1/stores/{id}/items/{itemID}` | 修改 / 删除草稿 |
| POST | `/v1/stores/{id}/items/batch-import` | 批量导入 `{items:[...]}`，最多 500 项；任一项非法整批回滚（单事务） |

草稿编辑不影响线上菜单，只有发布后才对屏幕可见。

### 发布（管理端）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/stores/{id}/publish` | `{expected_version}`。乐观并发：与当前版本不一致返回 409 `version_conflict`；同一版本号并发发布只有一个成功（门店行锁串行化）。发布生成**不可变**版本快照 |
| GET | `/v1/stores/{id}/versions` | 版本列表 |
| GET | `/v1/stores/{id}/versions/{n}` | 某版本的不可变内容 |

### 临时价格（管理端）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/stores/{id}/temp-prices` | `{item_id, price_cents, starts_at, ends_at}`，时间可带任意 UTC 偏移（如 `2026-09-20T18:00:00+08:00`），统一按 UTC 存储。区间**左闭右开**；同一菜品窗口重叠返回 409 `overlap`（由 PostgreSQL 排除约束 `EXCLUDE USING gist` 保证，并发写入无法绕过） |
| GET | `/v1/stores/{id}/temp-prices` | 窗口列表 |
| DELETE | `/v1/stores/{id}/temp-prices/{tpID}` | 删除窗口 |

临时价格随菜单一起下发，到点自动生效/失效（读取时按当前时间计算生效价），无需重新发布。

### 销售事件（管理端）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/stores/{id}/sales-events` | `{event_id, item_id, quantity, occurred_at}`。按 `event_id` 幂等：相同内容重放返回 200 `duplicate:true` 且不重复计数；同 ID 不同内容返回 409 `event_conflict`。销量按**门店时区**归入 `occurred_at` 对应的自然日（迟到事件归实际发生日）。达到菜品的 `sold_out_threshold` 自动售罄，跨日自动恢复 |
| GET | `/v1/stores/{id}/sales/daily?date=YYYY-MM-DD` | 某日分菜品销量 |

幂等由 `(store_id, event_id)` 主键 + 单事务内 `INSERT ... ON CONFLICT DO NOTHING`
与 `daily_sales` 原子累加保证：并发下不会重复计也不会漏计。

### 屏幕（管理端）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/stores/{id}/screens` | 创建屏幕，返回一次性 `token` |
| GET | `/v1/stores/{id}/screens` | 屏幕列表，含 `online`（90 秒无心跳视为离线） |

### 屏幕端

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/screen/menu` | 当前生效菜单（版本号、生效价、临时价格、售罄状态）。响应带 `ETag`；`If-None-Match` 命中返回 304。发布、临时价格到点、售罄翻转都会使 ETag 变化。菜单在单个 REPEATABLE READ 快照中组装，一次响应不会混入新旧状态。断线重连直接重新拉取本接口即得最新完整菜单 |
| POST | `/v1/screen/heartbeat` | 心跳，刷新在线状态 |

## 一致性设计要点

- **并发发布**：`SELECT ... FOR UPDATE` 锁定门店行后校验 `expected_version` 再插入
  `current_version+1`，`UNIQUE(store_id, version)` 兜底；同事务提交，版本与快照原子生效。
- **不可变版本**：`menu_version_items` 只插入不更新，历史版本可任意回放。
- **临时价格重叠**：数据库排除约束（`btree_gist`）在提交时强制非重叠，与应用校验无关。
- **销售幂等**：事件主键去重 + 内容哈希识别"同 ID 不同内容"；事件登记与日累计在同一事务。
- **缓存一致性**：ETag 由响应内容（版本、生效价、售罄集）哈希得出，任何影响展示的变化
  （包括时间驱动的临时价格切换）都改变 ETag；菜单在单一快照事务内读取，不混状态。

## 项目结构

```
cmd/signalboard/        入口（加载配置、跑迁移、起服务）
internal/config/        环境变量配置
internal/migrate/       内嵌 SQL 迁移（启动时自动执行）
internal/migrate/sql/   迁移文件（*.up.sql / *.down.sql）
internal/sqlc/          sqlc 查询
internal/db/            sqlc 生成代码（勿手改）
internal/server/        HTTP 层：路由、handler、集成测试
scripts/seed.sh         演示数据脚本
Dockerfile              多阶段构建，产出 distroless 静态镜像
docker-compose.yml      app + postgres
```
