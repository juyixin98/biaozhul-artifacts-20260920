# ConsentVault

可追溯的授权记录后端（FastAPI + SQLAlchemy 2 + PostgreSQL + Alembic + Docker）。

ConsentVault 以**事件溯源**方式记录主体对某组织、某目的的授权状态（授予 / 撤回 / 到期），
当前状态是不可变事件历史的一个投影，可随时从历史完整重建。

> **范围声明**：本系统只提供“可追溯的授权状态”记录能力（谁、对什么目的、基于哪个
> 政策版本、在何时授予或撤回）。**它不宣称满足或通过任何具体法规（如 GDPR、PIPL 等）
> 的合规认证**；是否合规取决于完整的数据处理流程、合同与法律评估。

## 核心语义

| 需求 | 实现 |
| --- | --- |
| 按组织 / 主体 / 目的记录 | 所有表与查询都带 `organization_id`；密钥绑定组织，结构上无法跨组织访问 |
| 授予 / 撤回 / 到期 | `consent_states.status`；到期在**读取时即时判定**（`expires_at <= now` 即失效），不依赖清理任务 |
| 政策版本发布后不可改 | `policy_versions` 上有数据库触发器，禁止 UPDATE/DELETE；每次授予绑定具体 `(政策, 版本号)` |
| 更新政策不自动续授权 | 已存在的授权永远指向授予时的版本号；发布 v2 不影响 v1 授权 |
| 事件 ID 幂等 | 同 `event_id` + 同请求体 → 返回首次结果；同 ID 不同内容 → `409 idempotency_mismatch` |
| 乐观并发 | 每个写请求携带 `expected_version`；过期并发写 → `409 version_conflict` |
| 失败不进历史 | 所有校验在同一事务内完成，任何错误整体回滚；`consent_events` 触发器禁止 UPDATE/DELETE/TRUNCATE |
| 撤回后不可被延迟重试恢复 | 旧事件重试只回放已存结果；状态只有**新的明确授予**才能恢复 |
| 从历史重建 | `POST /api/v1/rebuild` 删除投影并重放事件；重建持组织级咨询锁，与写入串行化，**事件不丢、不重放两次** |
| 批量导入 | 最多 500 条，单事务，任意错误整批回滚；批量内重复 event_id 也会冲突 |
| 主体删除（擦除） | 删除可识别映射、状态投影、导出副本，幂等快照去标识化；事件历史仅保留单向假名；审计日志从不含个人字段 |
| 权限分离 | `admin` 可写；`auditor` 只读；组织间数据互不可见 |

## 快速开始（Docker）

```bash
docker compose up -d --build          # db:55435, api: http://localhost:8010
docker compose --profile demo run --rm demo   # 端到端演示
docker compose --profile test run --rm test   # 容器内测试（37 用例）
```

启动后：

- 健康检查：`GET http://localhost:8010/healthz`
- 交互式文档：`http://localhost:8010/docs`
- 迁移在容器启动时自动执行（`alembic upgrade head`）。

> 主机端口 `55435`（Postgres）与 `8010`（API）是为避免与本机其他实例冲突而选的，
> 可在 `docker-compose.yml` 修改。compose 项目名固定为 `consentvault`。

## 本地开发

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
# 需要一个可达的 Postgres；.env 默认指向 compose 的 55435
alembic upgrade head
uvicorn app.main:app --reload --port 8010
pytest -q
```

## 1. 开通组织（管理密钥）

```bash
curl -s -X POST http://localhost:8010/management/organizations \
  -H "X-Management-Key: change-me-management-key" \
  -H "Content-Type: application/json" \
  -d '{"name":"acme"}'
# -> { organization_id, admin_api_key, auditor_api_key }
```

生产环境请通过环境变量设置独立密钥：
`CONSENTVAULT_MANAGEMENT_API_KEY`、`CONSENTVAULT_AUDITOR_API_KEY`，并设
`CONSENTVAULT_ENVIRONMENT=prod`（关闭演示用的兜底密钥）。

## 2. 配置目的与政策版本

```bash
ADMIN=...            # 上一步返回的 admin_api_key
curl -X POST localhost:8010/api/v1/purposes -H "X-API-Key: $ADMIN" \
  -H "Content-Type: application/json" -d '{"key":"marketing","description":""}'

curl -X POST localhost:8010/api/v1/purposes/marketing/policy-versions \
  -H "X-API-Key: $ADMIN" -H "Content-Type: application/json" -d '{"body":"v1 全文"}'
# -> version 1；再次发布得到 version 2，v1 行此后不可修改
```

## 3. 写入事件（携带 event_id + expected_version）

```bash
curl -X POST localhost:8010/api/v1/events/grant -H "X-API-Key: $ADMIN" \
  -H "Content-Type: application/json" -d '{
    "event_id": "01J...-grant",
    "expected_version": 0,
    "subject_ref": "user-123",
    "purpose_key": "marketing",
    "policy_version": 1,
    "expires_at": "2026-12-31T00:00:00Z"
  }'
```

- 首次：创建事件、推进投影到 version 1，返回当前视图与 `basis`（依据的事件序号、政策版本）。
- 同体重试：`replayed: true`，返回**首次的响应**，不产生新事件。
- 同 `event_id` 换内容：`409 idempotency_mismatch`。
- `expected_version` 过期：`409 version_conflict`。
- 撤回：`POST /api/v1/events/withdrawal`（`expected_version` 为当前版本）。
- 撤回后重放旧授予/旧撤回都不会改变状态；只有新的 `grant`（携带当前版本）恢复。

## 4. 验证

```bash
curl "localhost:8010/api/v1/verify?subject_ref=user-123&purpose_key=marketing" \
  -H "X-API-Key: $ADMIN"
```

```json
{
  "valid": true,
  "status": "granted",
  "reason": "active_grant",
  "state_version": 1,
  "basis": {
    "grant_event_id": 1, "grant_event_sequence": 1,
    "withdrawal_event_id": null, "withdrawal_event_sequence": null,
    "policy_version": 1, "policy_version_id": 1
  }
}
```

到期后无需任何后台任务，再次 `verify` 立即得到 `valid:false, reason:"expired"`
（边界含等号：`expires_at <= now` 即失效）。撤回为 `reason:"withdrawn"`，
无记录为 `status:"no_record"`（删除主体后查询同此结果）。

## 5. 其他端点

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/v1/events/batch` | admin | 1–500 条事件，整批原子提交/回滚，支持 grant 与 withdrawal |
| GET  | `/api/v1/history?subject_ref=&purpose_key=` | 两者 | 按序号返回不可变事件（不含可识别字段） |
| GET  | `/api/v1/history-by-pseudonym?pseudonym=&purpose_key=` | 两者 | 删除后凭审计日志中的假名继续审计保留的历史 |
| POST | `/api/v1/rebuild` | admin | 从历史重建当前投影 |
| POST | `/api/v1/subjects/{ref}/export-copies` | admin | 登记一份导出副本（删除时一并清除） |
| DELETE | `/api/v1/subjects/{ref}` | admin | 擦除可识别数据，保留假名历史与去标识审计 |
| GET  | `/api/v1/audit-logs` | 两者 | 操作审计（仅角色、动作、id/假名，无个人字段） |
| GET  | `/api/v1/purposes`、`GET /purposes/{key}/policy-versions` | 两者 | 读取配置 |

审计员密钥对所有写端点返回 `403`；任何密钥只能看到其所属组织的数据。

## 并发与一致性设计

- **咨询锁两级串行化**：每个写事务先取“组织锁”，再取“(主体,目的) 键锁”。
  重建事务在整个生命周期持有组织锁，因此重建与写入严格串行：写入要么在重放前提交
  （被重建包含），要么等锁释放后走正常增量路径 —— 不会丢事件，也不会重复。
- **乐观版本号**：`consent_states.version` 是该 (主体,目的) 已应用的事件数；
  并发撤回测试验证了两个竞争者恰有一个成功，另一个收到 409。
- **重建等价性**：重建出的 `status / version / 授予与撤回依据 / 时间戳` 与
  增量投影逐字段一致（见 `tests/test_rebuild.py`）。

## 主体擦除后的行为

- `subjects`、`consent_states`、`subject_export_copies` 物理删除；
  `event_idempotency` 的响应快照清空并打 `erased` 墓碑。
- `consent_events` 保留，但只有 `SHA-256` 假名（每次重新注册还会换盐，无法与新身份关联）。
- 旧 `event_id` 的延迟重试得到 `410 subject_erased`，**不会重建已删除主体**。
- 同一自然主体若重新注册，是一条全新的历史线（版本号从 0、序号从 1 开始）。

## 测试覆盖

`tests/`（共 37 个用例）覆盖：并发撤回/并发授予、到期边界（含恰好到期）、
政策更新不续授权、重复/冲突的批量导入、历史重建与增量等价、重建期间写入不丢失、
删除后查询/历史/重试结果、角色分离与组织隔离，以及事件表与政策版本表的不可变性。

## 目录结构

```
app/
  main.py            # FastAPI 应用、启动时安装不可变触发器
  config.py db.py    # 设置 / 引擎会话
  models.py          # ORM 模型
  schemas.py         # 请求/响应模型
  services.py        # 事件写入、幂等、OCC、验证、批量、擦除、审计
  services_rebuild.py# 从不可变历史重建投影
  auth.py management.py routers.py errors.py security.py locking.py
alembic/             # 迁移（建表 + 不可变触发器）
scripts/demo.py      # 端到端演示
tests/               # pytest 套件
docker/entrypoint.sh # 等待数据库 + 迁移 + 启动
```
