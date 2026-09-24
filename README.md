# 产物晋级原子性服务（atomicpromo）

纯后端实现：本地构建产物从**测试环境（test）到预发布环境（staging）**的原子晋级。
技术栈 Go 1.22 + [chi](https://github.com/go-chi/chi/v5) + PostgreSQL 14+。

## 1. 这个系统保证什么

| 需求 | 实现方式 |
|---|---|
| 晋级只引用不可变 digest | 所有晋级请求必须给 `sha256:<hex>`；标签/浮动名在入口被拒（`internal/promotion/service.go: validate`） |
| 钉死测试证据版本与策略版本 | 请求必须给 `(evidence_id, version)`、`(policy_id, version)`；证据与策略都是**只追加的不可变版本**，带 Ed25519 签名，服务端在晋级事务外重新独立验签 |
| 不能按浮动标签复制 | `tags` 表仅供检索；`PUT /tags/{name}` 可浮动，但 digest 不是 `sha256:hex` 形式的请求直接 400 |
| 并发晋级防覆盖 | 每个环境有单调 `gen`；提交事务以 `expected_gen` 做乐观 CAS（环境级 `pg_advisory_xact_lock` 串行化 + `WHERE gen=expected` 校验），失败方得到 `409/conflict`，绝不覆盖 |
| 复制校验完成后才切指针 | 真实字节复制到环境目录 → 对新文件**独立重新流式 sha256** → 校验通过后才在同一事务内写历史+切指针+消费审批+写收据 |
| 中断保留旧可用版本 | 复制失败/校验失败/事务提交前崩溃，`env_pointers` 永不前进；启动恢复把遗留尝试标记为 `recovered_aborted`（或崩溃发生在提交后的 `recovered_committed`），旧版本持续在线 |
| 回退只能指向完整且仍满足保留规则的历史产物 | 回退目标必须在 `env_history` 中、环境副本物理存在且重新哈希通过、且落在钉版策略 `keep_last_n` 保留集内；已被 GC 的副本拒绝回退（不从内容库偷偷复活） |
| 迟到审批 | 审批签名钉死完整元组含 `expected_gen`；代次已前进后旧审批被判 `conflict`，且**不被消费**（仍为 `valid` 留档） |
| 并发回退 | 与晋升同一代次 CAS + 同一把咨询锁；两个同代次回退恰好一个 committed、一个 conflict |
| 每次尝试完整证据 | `promotion_attempts` 对每次尝试记一行（成功/失败/冲突/恢复），含 src/dst 路径、复制后重新哈希摘要、failure_stage/reason、gen_before/after、服务端签名收据；永不更新删除终态 |
| 真实密码学 | 全部使用标准库 `crypto/sha256`、`crypto/ed25519`，无模拟；收据也由服务端 Ed25519 密钥签名，可离线验真 |

提交指针的事务（`internal/store/env.go: PromotionCommit`）在一个事务内完成：

```
pg_advisory_xact_lock(env) → 读 gen 并与 expected_gen 比对
  → (promote) 登记已校验副本 / (rollback) 确认副本仍在
  → 原子消费审批（valid→consumed，唯一部分索引保证只消费一次）
  → INSERT env_history(gen+1)
  → UPDATE env_pointers SET digest=新值, gen=gen+1 WHERE gen=expected
  → UPDATE promotion_attempts SET committed + 收据
```

任一步失败整体回滚；物理副本复制/校验发生在事务之外（大文件不应占住事务），
但只有校验通过的副本才会进入上面的事务。

## 2. 目录结构

```
cmd/
  server/          HTTP 服务入口（含启动恢复）
  keygen/          生成 Ed25519 密钥
  genexample/      真实生成 examples/ 全部签名输入（每次运行重新签名）
  signapproval/    对晋升元组做真实 Ed25519 审批签名
  verifyreceipt/   离线验签晋升/回退收据
internal/
  crypto/canon/    确定性 canonical JSON（签名/哈希前的规范化）
  crypto/sig/      SHA-256 流式哈希 + Ed25519 签名/验签
  blob/            内容寻址仓库 + 环境副本真实复制 + 复制后重新哈希 + 故障注入
  evidence/        测试证据版本（不可变，签名负载）
  policy/          策略版本（不可变，body_sha256，签名）
  approval/        审批元组（钉死代次）
  receipt/         服务端签名收据
  store/           PostgreSQL：schema 迁移 + 原子提交事务
  promotion/       编排：校验 → 复制 → 提交；回退/GC/恢复/幂等
  httpapi/         chi 路由与错误码映射
migrations/0001_init.sql
examples/          示例产物/证据/策略/审批（genexample 生成）
scripts/
  db-prepare.sh    本机 Postgres 建角色和库（或用 docker compose）
  accept.sh        一键验收（9 个场景、约 50 个断言）
docker-compose.yml 仅 Postgres
```

## 3. 本地启动

前置：Go 1.22+、PostgreSQL（本机 apt 安装或 docker compose 二选一）、curl/jq（验收脚本用）。

### 3.1 准备数据库（任选一种）

方式 A — 本机已有 PostgreSQL（peer 认证，apt 安装的常见情况）：

```bash
bash scripts/db-prepare.sh
# 创建角色 promo / 库 promo_atomic、promo_atomic_test
```

方式 B — Docker：

```bash
docker compose up -d          # 127.0.0.1:5432, promo/promo_dev_pwd
```

### 3.2 生成示例输入（真实签名）

```bash
go run ./cmd/genexample -out examples
# examples/artifacts/*.bin            三个产物
# examples/evidence-{a,b,c}-*.json    带 Ed25519 签名的测试证据（证据 a 有 v1/v2）
# examples/policy-v{1,2}.json         两个不可变策略版本（v2 要求安全测试+审批，keep_last_n=2）
# examples/approval-*.json            示例审批
# examples/keys/{ci,policy-authority,approver}.{pub,key}
```

### 3.3 启动服务

```bash
go run ./cmd/server
# 默认: HTTP_ADDR=:8080 （验收脚本用 18080）
# DSN 环境变量: DATABASE_URL 或 PGHOST/PGPORT/PGUSER/PGPASSWORD/PGDATABASE
# 产物仓库:     BLOB_STORE_DIR（默认 /tmp/atomicpromo-blobs）
# 收据签名密钥: SIGNING_KEY_DIR（首次启动自动生成）
```

可选故障注入开关（仅验收/测试用）：`ALLOW_FAULT_INJECTION=1`，开启后请求可带头：
`X-Test-Fault: corrupt-copy`（复制时翻转字节）或 `X-Test-Fault: crash-before-commit`（提交前 os.Exit）。

## 4. 验收命令

```bash
# 一键：建库 → 生成示例 → 起服务 → 全部场景 → 输出断言
bash scripts/accept.sh            # 默认端口 18080、库 promo_atomic
PORT=28471 PGDATABASE=promo_atomic_p099a bash scripts/accept.sh   # 与本机其他服务隔离运行
```

覆盖场景（脚本每步都有断言，失败如实非零退出）：

1. 不可变引用：错误声明 digest 被拒、`sha256sum` 交叉核对、伪造证据签名被拒、浮动标签晋升被拒、缺必需测试被拒
2. happy path：真实晋级 + 收据 `cmd/verifyreceipt` 离线 Ed25519 验真
3. 幂等键重放返回同一 attempt；同一审批签名不可二次消费
4. 并发晋级：两个相同 `expected_gen` 请求恰好 1 committed / 1 conflict，代次只前进 1
5. 迟到审批：钉旧代次的审批被 conflict 拒绝，库里审批保持 `valid`
6. 复制失败：故障翻转字节 → 复制后重新 sha256 不过 → `copy_failed`，旧指针/旧代次不变，失败尝试证据完整，审批未被消费；随后无故障重试成功
7. 指针提交前崩溃：进程 `os.Exit(99)` → 重启恢复为 `recovered_aborted`，环境仍是空/旧版本 → 同 expected_gen 重试成功
8. 回退与并发回退（1 committed/1 conflict）、回退未知 digest 被拒
9. GC：按 `keep_last_n=2` 物理删除超保留副本；回退被 GC 版本被 409；回退保留集内版本成功
10. 审计：`/attempts` 中每次尝试都有终态、failure_stage、复制校验摘要

### 手工 smoke（不用脚本时）

```bash
go run ./cmd/server &
# 上传
DA=$(sha256sum examples/artifacts/a.bin | cut -d' ' -f1)
curl -sS -X POST localhost:8080/v1/artifacts -F content=@examples/artifacts/a.bin
curl -sS -X POST localhost:8080/v1/evidence -H 'Content-Type: application/json' \
  --data @examples/evidence-a-v2.json
curl -sS -X POST localhost:8080/v1/policies -H 'Content-Type: application/json' \
  --data @examples/policy-v2.json
curl -sS -X POST localhost:8080/v1/approvals -H 'Content-Type: application/json' \
  --data @examples/approval-a-staging-gen0.json
# 晋级（注意只接受精确 digest/版本）
curl -sS -X POST localhost:8080/v1/promotions -H 'Content-Type: application/json' -d '{
  "env":"staging","digest":"sha256:'$DA'",
  "evidence_id":"ev-a","evidence_version":2,
  "policy_id":"promotion-policy","policy_version":2,
  "expected_gen":0,"approval_id":"apr-a-staging-gen0"}'
curl -sS localhost:8080/v1/envs/staging
```

## 5. API 摘要

| 方法/路径 | 说明 |
|---|---|
| `POST /v1/artifacts` | multipart 上传，服务端流式重算 sha256（可带 `digest` 做声明校验） |
| `POST /v1/evidence` | 登记不可变证据版本（验签，重号 409） |
| `POST /v1/policies` | 登记不可变策略版本（验签，返回 body_sha256） |
| `PUT/GET /v1/tags/{name}` | 浮动标签（仅检索；晋升接口拒绝） |
| `POST /v1/promotions` | 晋级；body 钉死 digest/evidence version/policy version/expected_gen，可选 approval_id、idempotency_key |
| `POST /v1/approvals` | 登记审批（验签，钉死完整元组含 expected_gen） |
| `POST /v1/promotions/approve` | 用已登记审批完成晋级（两阶段；迟到审批在此被识别） |
| `POST /v1/rollbacks` | 回退（digest 或 target_gen 二选一；重检保留规则/完整性） |
| `POST /v1/gc` | 按钉版策略回收环境副本 |
| `GET /v1/envs/{env}` | 当前指针 + gen + 完整历史 |
| `GET /v1/attempts/{id}`、`GET /v1/envs/{env}/attempts` | 每次尝试的完整证据 |
| `POST /v1/admin/recover` | 手工触发启动恢复逻辑 |

状态码：`201` 提交成功；`200` rejected/conflict/copy_failed/awaiting_approval 等带尝试记录的结果；
`400` 形状/不可变引用错误；`409` 回退目标/保留规则冲突；`422` 验签或策略拒绝。

## 6. 自动化测试

```bash
# 纯单元（canonical JSON、Ed25519、blob 复制校验/损坏检测）
go test ./internal/crypto/... ./internal/blob/...

# PostgreSQL 集成（默认连 postgres://promo:promo_dev_pwd@127.0.0.1:5432/promo_atomic_test）
go test -race -count=1 ./internal/promotion/...
# 覆盖：happy path+收据验真、标签拒绝、必需测试、并发 CAS、复制失败保旧指针、
#       迟到审批、回退规则、GC 后拒绝回退、幂等、崩溃恢复重试、并发回退
TEST_DATABASE_URL='postgres://...' go test ./...    # 自定义测试 DSN
```

## 7. 关键失败语义（对照验收）

- **测试复制失败**：副本字节在临时文件阶段被重新哈希发现不一致 → 删除临时文件，尝试置
  `copy_failed`（`failure_stage=copy`、记录原因/dst 路径），指针不动，审批不消费，可按同
  `expected_gen` 重试。
- **指针提交前崩溃**：`X-Test-Fault: crash-before-commit` 让进程在副本校验后、事务提交前
  `os.Exit(99)`。已校验副本留在环境目录，尝试留为 `in_progress`。重启时
  `RecoverOnStartup` 判定指针未变 → `recovered_aborted`；重试幂等（副本重新哈希复用）。
- **迟到审批**：审批签名的 `expected_gen` 与当前代次不符 → 结果 `conflict`，审批保持
  `valid`，环境指针不变。
- **并发回退/晋级**：后提交方在事务内发现 gen 已变 → 整体回滚并返回 `conflict`，其副本
  与尝试证据仍保留，可由调用方用新代次重新发起。

## 8. 设计取舍与边界

- 单实例 Postgres + 咨询锁足够表达“同一环境指针串行切换”；跨库/多区域一致性不在范围内。
- 内容库原件不做自动 GC（`artifacts` 与对象保留）；`gc` 只回收**环境副本**，历史行与尝试
  证据永不删除。
- 证据单签（`min_signers` 仅允许 0/1）；多签者门槛可以在不可变策略新版本中扩展。
- 收据在指针事务**之前**签名、与提交同一事务落库，因此“已落库的收据”与“已切换的指针”
  要么都成立要么都不成立。
