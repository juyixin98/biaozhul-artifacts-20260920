# ConsentVault — 可追溯的同意记录后端

用 **FastAPI + SQLAlchemy + PostgreSQL** 实现的同意（consent）记录服务。

> **免责声明**：本系统只负责以可追溯、可审计的方式**记录和报告授权状态**
> （授予 / 撤回 / 到期）。它不声明满足、也不构成任何具体法规（如 GDPR、
> ePrivacy、PIPL 等）的合规认证。是否合规取决于部署方的整体流程与法务评估。

## 核心语义

| 需求 | 实现方式 |
| --- | --- |
| 按组织 / 主体 / 目的记录同意 | 每个 `(organization, subject, purpose)` 是一条独立的事件流 |
| 授予、撤回、到期 | `grant` / `withdraw` 事件 + 读取时即时判定到期，**不依赖任何清理任务** |
| 政策版本发布后不可修改 | `policy_versions` 只追加；应用层拒绝重复发布，**数据库触发器拒绝任何 UPDATE/DELETE** |
| 授予绑定具体政策版本 | 每次 grant 外键绑定当时发布的版本；发布新版本**不会**自动续授权 |
| 幂等写入 | 请求携带 `event_id` + 语义指纹（规范化 JSON 哈希）；同 ID 重复请求返回原结果（`replayed: true`）；同 ID 内容不同返回 `409` |
| 乐观并发 | 请求携带 `expected_version`，过期返回 `409`；同一事件流用 PostgreSQL 事务级 advisory lock 串行化 |
| 失败事件不进历史 | 所有校验在提交前完成，失败即回滚 |
| 撤回后防延迟重试复活 | 旧 grant 的重放只返回原事件，撤回状态不变；只有**新的、显式的 grant 事件**才能恢复 |
| 从不可变历史重建 | `consent_events` 只追加（触发器保护），`consent_states` 由同一套纯函数 `fold_stream` 重建，结果与增量处理一致；重建时对事件表加 `EXCLUSIVE` 锁，**重建期间写入排队、事件不丢失** |
| 批量导入 | 最多 500 条，单事务提交；任意错误整批回滚；重复导入逐条标记 `replayed` |
| 主体删除（擦除） | 清除身份字段与外部标识映射、删除全部导出副本；审计仅保留不含个人字段的操作记录；事件账本以匿名数字 id 保留 |
| 权限分离 | `admin` 可写；`auditor` 只读；密钥只存 SHA-256 哈希 |
| 组织隔离 | 所有查询都带 `organization_id` 过滤，跨组织访问返回 404 |

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后：

- API：<http://localhost:8000>（OpenAPI 文档 `/docs`）
- 健康检查：`GET /healthz`
- 首次启动会自动执行 Alembic 迁移并写入演示组织 / 密钥 / 政策
  （`SEED_DEMO_DATA=1`，仅用于演示）

演示密钥：

| 组织 | 角色 | X-API-Key |
| --- | --- | --- |
| Demo Org A | admin | `demo-admin-key` |
| Demo Org A | auditor（只读） | `demo-auditor-key` |
| Demo Org B | admin | `demo-admin-key-b` |
| Demo Org B | auditor（只读） | `demo-auditor-key-b` |

### 跑演示脚本

```bash
# 服务启动后另开终端
python -m app.scripts.demo
```

演示覆盖：政策不可改、幂等授予、到期边界、撤回后旧授予不能复活、
发布新政策不续旧授权、历史重建、批量导入与重复导入、审计员只读、
跨组织隔离、擦除后的查询结果。

### 本地（无 Docker，需自备 PostgreSQL）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql+psycopg2://USER:PASS@localhost:5432/DBNAME'
alembic upgrade head
python -m app.scripts.seed
uvicorn app.main:app --reload
```

## API 摘要（均在 `/v1` 前缀下，需要 `X-API-Key` 头）

### 主体

- `POST /subjects`（admin）创建主体
- `GET /subjects/{id}`（只读）按内部 id 查询
- `GET /subjects/by-ref/{external_ref}`（只读）按外部标识解析（擦除后 404）
- `POST /subjects/{id}/exports`（admin）登记一次数据导出（保存导出副本）
- `POST /subjects/{id}/erase`（admin）擦除身份信息与导出副本

### 政策

- `POST /policies`（admin）发布新版本（不可再改）
- `GET /policies`（只读）列出版本

### 同意

- `POST /consent/grant`（admin）授予，body：
  ```json
  {
    "event_id": "evt-001",
    "subject_id": 1,
    "purpose": "marketing",
    "expected_version": 0,
    "policy_version": "v2026-01",
    "expires_at": "2030-01-01T00:00:00+00:00"
  }
  ```
- `POST /consent/withdraw`（admin）撤回（body 含 `event_id` / `expected_version`）
- `GET /consent/{subject_id}/{purpose}/verify`（只读）返回当前是否有效及依据：
  ```json
  {
    "valid": true,
    "reason": "valid",
    "current_version": 3,
    "grant_event_id": "evt-...",
    "withdraw_event_id": null,
    "policy_version": "v2026-01",
    "expires_at": "2030-01-01T00:00:00+00:00",
    "evaluated_at": "2026-09-20T..."
  }
  ```
  `reason` 取值：`valid` / `expired` / `withdrawn` / `no_consent_event`
- `GET /subjects/{id}/history`（只读）该主体的不可变事件历史
- `POST /consent/batch`（admin）批量导入（1–500 条，单事务原子提交）
- `POST /admin/rebuild`（admin）从事件历史重建物化状态表

### 审计

- `GET /audit-logs`（只读）操作审计流（仅组织内、不含个人字段）

错误响应统一为 `{"error": "<code>", "detail": "..."}`，
冲突类为 `409`，语义非法为 `422`，主体已擦除为 `410`，
跨组织 / 不存在为 `404`。

## 数据模型要点

```
organizations
api_keys(organization_id, key_hash[sha256], role[admin|auditor], active)
subjects(organization_id, external_ref[擦除时清空], email, display_name, erased, erased_at)
export_copies(organization_id, subject_id, destination, payload)   # 擦除时删除
policy_versions(organization_id, version, body, published_at)      # 触发器: 禁 UPDATE/DELETE
consent_events(organization_id, event_id, subject_id, purpose,
               event_type[grant|withdraw], version,
               policy_version_id, expires_at, fingerprint)         # 触发器: 禁 UPDATE/DELETE
consent_states(organization_id, subject_id, purpose, last_event_id, version)  # 可重建
audit_logs(organization_id, api_key_id, action, detail[JSON 无个人字段], created_at)
```

- 唯一约束：`(org, event_id)`（幂等键）、`(org, subject, purpose, version)`（流版本）。
- 指纹：对 `event_id / subject_id / purpose / expected_version / event_type /
  policy_version / expires_at` 做规范化 JSON 后 SHA-256；同一 `event_id`
  指纹不同即冲突。
- 并发：事件流用 `pg_advisory_xact_lock(sha256(stream))` 串行化；
  重建在单事务内 `LOCK TABLE consent_events IN EXCLUSIVE MODE` 后排他替换
  `consent_states`，写入方阻塞到重建提交后继续，事件零丢失。

## 迁移

```bash
alembic upgrade head     # 升级
alembic downgrade -1     # 回滚（同时移除触发器）
```

初始迁移 `0001_initial` 建表并安装两个 append-only 触发器：

```sql
CREATE TRIGGER trg_consent_events_no_update
  BEFORE UPDATE OR DELETE ON consent_events
  FOR EACH ROW EXECUTE FUNCTION consentvault_block_mutation();
CREATE TRIGGER trg_policy_versions_no_update
  BEFORE UPDATE OR DELETE ON policy_versions
  FOR EACH ROW EXECUTE FUNCTION consentvault_block_mutation();
```

## 测试

测试通过 search_path 为每个用例创建独立 PostgreSQL schema，互不干扰。

```bash
# 先启动数据库（可用 compose 只起 db）
docker compose up -d db

# 安装依赖后（建议虚拟环境）
TEST_DATABASE_URL='postgresql+psycopg2://consent:consent@localhost:5432/consentvault' \
  python -m pytest -v
```

覆盖场景（`tests/`）：

- `test_concurrency.py` — 并发撤回只有一个成功、过期版本冲突；撤回后旧授予
  重试不能复活，只有新显式授予恢复
- `test_expiry.py` — 到期前 1 微秒有效、到期瞬间立即失效、无清理任务参与
- `test_policy.py` — 已发布政策应用层与数据库触发器双重不可修改；新政策不续
  旧授权；授予必须引用已发布版本；失败事件不进历史
- `test_idempotency.py` — 同 ID 同内容返回原结果；同 ID 异内容 409；
  `expected_version` 过期 409
- `test_batch.py` — 500 上限、整批回滚、批内重复 ID 冲突、重复导入全量
  replay、批内 grant→withdraw 多版本
- `test_rebuild.py` — 重建结果与逐事件折叠一致；人为篡改物化表后重建修复；
  重建与 8 路并发写入同时进行，事件不丢失、最终状态一致
- `test_erasure_permissions.py` — 擦除清除映射 / 导出副本、保留 PII-free
  账本与审计；擦除后按外部标识 404；审计员只读；缺 / 错密钥 401；
  组织间不可互查
