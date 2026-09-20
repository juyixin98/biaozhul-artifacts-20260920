# DeskLens 活动汇总后端

DeskLens 接收工作站每分钟的活动快照（**不采集原始击键**，只有应用名 + 活动计数），
按隐私策略过滤后落库，分类为生产性 / 非生产性 / 中性，并预聚合出员工每日、部门每周汇总。

技术栈：Go · Echo · sqlx · PostgreSQL · Docker Compose。

## 快速开始

```bash
docker compose up -d --build     # 应用: http://localhost:18095  数据库: localhost:55441
```

启动时自动执行迁移（`migrations/`），并在 `SEED_DEMO_DATA=true` 时灌入演示数据
（部门、员工、账号、策略 v1、分类规则 v1）。

演示账号（`Authorization: Bearer <token>`）：

| token             | 角色    | 范围                |
|-------------------|---------|---------------------|
| `admin-token`     | admin   | 全部 + 管理接口     |
| `ingest-token`    | ingest  | 仅快照上报          |
| `mgr-eng-token`   | manager | 仅 Engineering 部门 |
| `mgr-sales-token` | manager | 仅 Sales 部门       |

本地开发：

```bash
go test ./...                     # 单元测试（无需数据库）
docker run -d --name desklens-test-pg -p 55440:5432 \
  -e POSTGRES_USER=desklens -e POSTGRES_PASSWORD=desklens -e POSTGRES_DB=desklens postgres:16-alpine
TEST_DATABASE_URL='postgres://desklens:desklens@localhost:55440/desklens?sslmode=disable' go test ./...
DATABASE_URL='postgres://desklens:desklens@localhost:55440/desklens?sslmode=disable' go run ./cmd/server
```

## 核心设计

### 幂等与冲突
- 幂等键 = `(workstation_id, minute_utc)`（数据库唯一约束）。
- 每条快照计算内容哈希（员工、分钟、应用、计数）。同键同内容 → 记为 duplicate，只处理一次；
  同键不同内容 → 整批返回 **409** 并回滚。
- 任何校验失败（缺字段、负计数、未知员工）→ **400**，整批回滚，不产生部分写入。

### 隐私过滤（摄取时，过滤数据永不落库）
当前发布的策略版本决定：员工本地时区的监控窗口（默认 09:00–18:00、周一至周五）、
排除应用列表、豁免部门。被过滤的快照**不进入原始表，也不进入任何统计**。
每条原始记录都盖上实际采用的 `policy_version`；发布新策略只影响之后摄取的数据。

### 应用分类（版本化）
本地通配规则（`*`、`?`，大小写不敏感）把应用分为 productive / unproductive / neutral。
优先级高者胜；**优先级相同按规则 ID 升序**，保证稳定匹配。未命中归为 neutral。
每条原始记录保存 `classification_version` 与当时的分类结果——规则改版不会悄悄改写历史。

### 聚合与迟到数据
- 每条原始行归属员工的**本地日历日**（`(minute_utc AT TIME ZONE tz)::date`），周汇总按 ISO 周一。
- 汇总采用**按范围整体重算 + UPSERT**：摄取事务内只重算受影响的 (员工, 日) 及其所在周。
  因为重算是全量而非增量累加，从原始记录重建的结果与增量处理**必然一致**；
  重建与新写入并发时，双方在汇总行唯一键上串行化，不会丢失或重复累计。
- 迟到快照只触发其所属日期的重算，不影响其他日期。

### 清理与可重建范围
- `POST /api/v1/admin/cleanup` 删除指定时间之前的原始行，汇总保留，并推进
  `retention_state.raw_cutoff`（可重建范围下界）。
- `POST /api/v1/admin/rebuild` 拒绝任何触及 cutoff 之前的范围（**422**），
  防止用残缺历史覆盖完整统计。

### 权限隔离
Bearer token → 用户（admin / manager / ingest）。manager 只能读取本部门的员工与日/周汇总，
**明细接口与 CSV 导出走同一套隔离检查**；ingest 角色只能上报；管理接口仅 admin。

## API 一览

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | `/api/v1/snapshots/batch` | ingest/admin | 批量上报快照 |
| GET  | `/api/v1/employees/:id/daily?from=&to=` | admin/manager | 员工每日汇总 |
| GET  | `/api/v1/employees/:id/snapshots?date=` | admin/manager | 原始明细（受清理边界限制） |
| GET  | `/api/v1/departments/:id/weekly?from=&to=` | admin/manager | 部门每周汇总 |
| GET  | `/api/v1/export/employees/:id/daily.csv?from=&to=` | admin/manager | CSV 导出 |
| GET  | `/api/v1/export/departments/:id/weekly.csv?from=&to=` | admin/manager | CSV 导出 |
| POST | `/api/v1/admin/policies` | admin | 发布新策略版本 |
| POST | `/api/v1/admin/classifications` | admin | 发布新分类规则版本 |
| POST | `/api/v1/admin/rebuild` `{from,to}` | admin | 从原始记录重建汇总 |
| POST | `/api/v1/admin/cleanup` `{before}` | admin | 清理原始数据 |
| GET  | `/healthz` | - | 健康检查 |

### 示例

```bash
# 上报一批快照（含一条被策略过滤的数据）
curl -X POST http://localhost:18095/api/v1/snapshots/batch \
  -H 'Authorization: Bearer ingest-token' -H 'Content-Type: application/json' -d '{
  "snapshots": [
    {"workstation_id":"ws-alice","employee_id":1,"captured_at":"2026-09-18T01:30:00Z","app_name":"Visual Studio Code","activity_count":42},
    {"workstation_id":"ws-alice","employee_id":1,"captured_at":"2026-09-18T01:32:00Z","app_name":"1Password","activity_count":9}
  ]}'
# => {"received":2,"inserted":1,"duplicates":0,"filtered":1}

# 经理查看本部门员工每日汇总
curl -H 'Authorization: Bearer mgr-eng-token' \
  'http://localhost:18095/api/v1/employees/1/daily?from=2026-09-14&to=2026-09-20'

# 发布新分类规则（旧数据保留旧版本分类）
curl -X POST http://localhost:18095/api/v1/admin/classifications \
  -H 'Authorization: Bearer admin-token' -H 'Content-Type: application/json' -d '{
  "rules": [
    {"pattern":"*Code*","category":"productive","priority":10},
    {"pattern":"*YouTube*","category":"unproductive","priority":10},
    {"pattern":"*","category":"neutral","priority":0}
  ]}'

# 清理 2026-09-15 之前的原始数据（汇总保留）
curl -X POST http://localhost:18095/api/v1/admin/cleanup \
  -H 'Authorization: Bearer admin-token' -H 'Content-Type: application/json' \
  -d '{"before":"2026-09-15T00:00:00Z"}'
```

## 测试覆盖

- `internal/classify`：通配匹配、优先级并列时按规则 ID 稳定胜出、默认中性。
- `internal/policy`：监控窗口、时区换算、排除应用、豁免部门。
- `internal/store`（集成，需 `TEST_DATABASE_URL`）：隐私过滤不落库、跨日（UTC→本地日）、
  重复/冲突/整批回滚、迟到补传只重算受影响日、分类规则版本不改写历史、
  重建与摄取并发不丢不重、清理边界与重建拒绝。
- `internal/httpapi`（集成）：未认证 401、经理跨部门 403（含明细与导出）、
  角色不足 403、批量校验 400、冲突 409。

## 目录结构

```
cmd/server/          入口（连接重试、迁移、可选种子数据）
migrations/          SQL 迁移（内嵌进二进制，启动时按序执行）
seed/seed.sql        演示数据（SEED_DEMO_DATA=true 时执行）
internal/model/      领域类型
internal/policy/     隐私/监控策略（纯函数）
internal/classify/   通配分类规则（纯函数）
internal/store/      摄取、聚合、重建、清理（sqlx）
internal/httpapi/    Echo 路由、认证、部门隔离、CSV 导出
```
