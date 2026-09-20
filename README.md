# SIRCC — 安全事件响应后端

Go + Chi + sqlc + PostgreSQL 的事件响应后端。不连接任何真实安全设备或外部服务：鉴权为请求头 `X-User-Id`（种子用户见 `docs/api.md`），提醒由内置调度器持久化到数据库。

## 功能

- 生命周期：`detected → triaged → contained → eradicated → recovered → postmortem → closed`，每次只能前进一个阶段。
- P1 未分配响应人员不能完成分诊；关闭前必须有根因、经验教训及带负责人和期限的行动项。
- 阶段转换携带 `expectedVersion` + `requestId`：重复请求返回原结果，过期版本 409 拒绝；状态、阶段时间、审计事件同事务写入。
- 角色：analyst（分诊/证据）、responder（推进处置，须为本案 assignee）、admin（分配人员）。
- 证据每案最多 50 条（行锁下计数，并发无法突破），提交后不可覆盖，纠正只能追加关联说明。
- 行动项到期产生持久化提醒，同一到期版本只提醒一次；改期作废旧调度；重启后自动补处理。
- 导出包含阶段记录、证据摘要和基于真实阶段时间的遏制/解决时长（未完成阶段为 null）。

## 快速开始（Docker）

```bash
sudo docker compose up --build
# API 在 http://localhost:18082（迁移在启动时自动应用）
./samples/demo.sh            # 端到端走一遍完整生命周期
```

## 本地开发

```bash
# 需要 PostgreSQL（连接串可用 DATABASE_URL 覆盖）
createdb sircc || true
go run ./cmd/server          # 监听 :8080，自动执行迁移

# sqlc 代码生成（修改 db/queries 或迁移后）
$(go env GOPATH)/bin/sqlc generate

# 集成测试（默认连 sircc_test 库，可用 TEST_DATABASE_URL 覆盖）
createdb sircc_test || true
go test ./...
```

## 布局

```
cmd/server/            入口：连接池、迁移、HTTP、提醒调度器
internal/migrate/      嵌入式 SQL 迁移（migrations/ 为唯一 schema 来源）
internal/store/        sqlc 生成代码（勿手改）
internal/httpapi/      Chi 路由、鉴权、状态机、证据、行动项、导出
internal/reminder/     到期提醒调度器（持久化、幂等、重启补处理）
db/queries/            sqlc 查询
docs/api.md            接口文档
samples/demo.sh        端到端样例
```

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgres://sircc:sircc@127.0.0.1:5432/sircc?sslmode=disable` | 连接串 |
| `ADDR` | `:8080` | 监听地址 |
| `REMINDER_INTERVAL` | `10s` | 提醒扫描间隔 |
| `TEST_DATABASE_URL` | `postgres://sircc:sircc@127.0.0.1:5432/sircc_test?sslmode=disable` | 测试库 |
