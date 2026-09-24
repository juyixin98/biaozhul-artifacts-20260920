# 产物晋级原子性服务（artifact-promotion）

本地构建产物从 **test → staging（预发布）** 的晋级后端。Go + Chi + PostgreSQL，纯后端 REST API。

## 核心保证

| 要求 | 实现机制 |
|---|---|
| 不可变引用 | 产物只以 `sha256:<64 hex>` digest 引用；证据、策略按 `(id, version)` 不可变版本引用。**不存在任何按浮动标签复制的路径**（`blob.ValidateDigest` 在入口拒绝一切非 digest 形式）。 |
| 复制校验后再切换指针 | 晋级先把 blob 真实复制到目标环境命名空间，**重算写入字节的 sha256 并与期望 digest 比对**，通过后才进入指针提交事务。 |
| 并发晋级防覆盖 | 指针切换是单条乐观并发 UPDATE：`UPDATE environments ... WHERE name=$ AND generation=$expected`。代次不匹配 → 0 行 → `generation_conflict`，败方完整证据落库。 |
| 中断保留旧可用版本 | 指针切换、历史追加、审批消费、尝试成功记录**在同一个数据库事务**内提交。提交前崩溃 → 尝试停留在 `running`，旧指针不变；启动时调和器把遗留 `running` 标记为 `interrupted` 并追加证据步骤。 |
| 回退约束 | 回退目标必须：① 在该环境指针历史（`env_history`）中；② 满足保留规则（最近 N 代之内 且 未超最大保留天数）；③ 磁盘 blob 当场重算 sha256 通过（完整性）。三者任一不满足即拒绝。 |
| 迟到审批 | 审批签发时绑定环境当前代次；环境前进后审批即 `late_approval`，且审批单次消费（`approval_consumed`）。 |
| 完整证据 | 每次尝试（ingest/promotion/rollback）的每个步骤实时追加到 `attempts.steps`（JSONB），成功、失败、中断均可在 `GET /v1/attempts/{id}` 检索。 |

## 指针提交事务（原子性核心）

```
BEGIN
  UPDATE environments SET current_digest=$d, generation=generation+1
    WHERE name=$env AND generation=$expected   -- 0 行 ⇒ 冲突，整体回滚
  INSERT INTO env_history (...)                 -- 追加式指针历史
  UPDATE approvals SET consumed_at=now() WHERE id=$ AND consumed_at IS NULL
  UPDATE attempts SET status='succeeded', steps=steps||... 
COMMIT                                            -- 要么全生效，要么旧版本继续服务
```

## 目录结构

```
cmd/server/main.go          入口：连接 PG、跑迁移、调和中断尝试、起 HTTP
internal/api/               Chi 路由与 JSON 编解码
internal/core/              晋级引擎（校验链、复制、原子提交、回退、调和）
internal/blob/              内容寻址 blob 存储（流式 sha256、原子 rename、复制校验）
internal/migrate/           嵌入式 SQL 迁移
internal/migrate/sql/       0001_init.sql
examples/                   示例输入 JSON + 可执行脚本（demo.sh / failure-cases.sh）
```

## 本地启动

前置：Go ≥ 1.22，本机 PostgreSQL（或远端，改 `DATABASE_URL` 即可）。

```bash
# 1. 建角色与数据库（已存在则跳过）
sudo -u postgres psql -c "CREATE ROLE promote LOGIN PASSWORD 'promote';"
sudo -u postgres createdb -O promote promotion_b
sudo -u postgres psql -d promotion_b -c "ALTER SCHEMA public OWNER TO promote;"

# 2. 启动（迁移自动执行；遗留 running 尝试自动调和为 interrupted）
go run ./cmd/server
# 环境变量：DATABASE_URL / BLOB_ROOT / LISTEN_ADDR（默认 :8080）
```

## 验收命令

```bash
# 自动化测试（真实 PostgreSQL + 真实 sha256/复制/事务，无任何 mock）
sudo -u postgres createdb -O promote promotion_b_test      # 一次性
sudo -u postgres createdb -O promote promotion_b_api_test  # 一次性
sudo -u postgres psql -d promotion_b_test     -c "ALTER SCHEMA public OWNER TO promote;"
sudo -u postgres psql -d promotion_b_api_test -c "ALTER SCHEMA public OWNER TO promote;"
go test ./... -count=1

# 端到端冒烟（服务器运行中）
bash examples/demo.sh            # 晋级 v1 → 晋级 v2 → 回退 v1 → 历史评估
BLOB_ROOT=./data/blobs bash examples/failure-cases.sh
#   1) 过期 expected_generation → generation_conflict
#   2) 并发晋级 → 恰好一个成功
#   3) 复制失败 → 指针不动
#   4) 迟到审批 → late_approval
#   5) 全部尝试证据可检索
```

测试覆盖（`internal/core/service_test.go`、`internal/api/server_test.go`、`internal/blob/store_test.go`）：

- `TestPromoteHappyPath` — 完整晋级 + 证据步骤 + 历史
- `TestPromoteConcurrentConflict` — 并发晋级，恰好一个成功，败方 `generation_conflict`
- `TestPromoteCopyFailure` — 源 blob 缺失 → `copy_failed`，指针不变
- `TestPromoteCrashBeforeCommit` — 提交前崩溃（故障注入 hook）→ 尝试 `running` → 调和为 `interrupted`，指针不变
- `TestPromoteLateApproval` — 迟到审批拒绝
- `TestRollbackHappyPath / RetentionExceeded / IncompleteArtifact / NotInHistory / Concurrent` — 回退全部分支
- `TestPromoteEvidenceFailures` — 未通过证据 / 证据版本过低
- `TestApprovalSingleUse` — 审批不可重放
- `TestAPIEndToEnd` — HTTP 层全流程（httptest + 真实 PG）
- blob 层：篡改检测、缺blob复制失败、标签形式拒绝

## API 一览（`/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/envs` | 创建环境（含保留规则 `retention_keep` / `retention_max_age_days`） |
| GET | `/envs` `/envs/{env}` | 环境当前指针与代次 |
| GET | `/envs/{env}/history` | 指针历史，含 `current`/`complete`/`retention_ok` 实时评估 |
| POST | `/envs/{env}/artifacts?expected_generation=N` | 上传产物原始字节（ingest）：算 digest、存 blob、原子移动指针 |
| POST | `/evidence` | 登记测试证据（版本自增） |
| POST | `/policies` | 登记晋级策略版本 |
| POST | `/approvals` | 签发审批（绑定当前代次） |
| POST | `/promotions` | 晋级（体见 `examples/promotion.json`）；领域拒绝返回 409 + 完整尝试证据 |
| POST | `/rollbacks` | 回退（体见 `examples/rollback.json`） |
| GET | `/attempts` `/attempts/{id}` | 尝试证据检索（可按 `kind`/`status` 过滤） |

## 失败语义

| `failure_reason` | 含义 |
|---|---|
| `policy_not_found` / `policy_mismatch` | 策略版本不存在或不管辖该环境对 |
| `evidence_not_found` / `evidence_digest_mismatch` / `evidence_not_passed` / `evidence_suite_mismatch` / `evidence_version_too_low` | 证据不满足策略 |
| `source_pointer_mismatch` | 源环境当前运行的不是该 digest |
| `approval_not_found` / `approval_env_mismatch` / `approval_digest_mismatch` / `approval_consumed` / `late_approval` | 审批无效、已消费或已迟到 |
| `copy_failed` | 复制或复制后 digest 校验失败 |
| `generation_conflict` | 预期代次已过期（并发晋级/回退败方） |
| `not_in_history` / `retention_exceeded` / `blob_incomplete` / `already_current` | 回退目标不合法 |
