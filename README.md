# 可撤销凭证索引（Revocable Credential Index）

纯后端服务：使用 **Go + Chi + PostgreSQL** 实现本地签名凭证的发行、撤销索引与
**按查询时间历史重放**的验证。所有身份与密钥均为本地生成的**合成测试数据**。

## 核心语义（这些是本系统的设计契约）

1. **本地真实签名**：发行时用签发者当前生效的 Ed25519 密钥（RFC 8032，
   `crypto/ed25519`）对确定性编码（canonical JSON）的载荷做真实签名；验证时用
   存储的历史公钥重新验签。签名、SHA-256 内容摘要全部真实计算，没有桩实现。
2. **撤销只追加**：撤销是 `revocations` 表中的**不可变事件**（每凭证至多一条，
   数据库唯一索引保证）。从不更新/删除凭证行，不用“当前状态”回答历史问题。
3. **历史重放**：`POST /v1/verify` 接受 `as_of`，判定只依据 `as_of` 时刻之前
   （含边界）已存在/已生效的事件：
   - 撤销：`as_of >= revocation.effective_at` 才成立（支持补录的、生效时间在
     过去的撤销；也支持未来定时生效的撤销事件）；
   - 有效期：`[not_before, expires_at)`，**到期那一瞬即为 EXPIRED**；
   - 签发时刻：`as_of < issued_at` 时凭证对历史观察者尚不存在（UNKNOWN）。
4. **全局快照号**：每一次状态变更（建签发者、轮换/停用密钥、发行、撤销）都从
   同一个 Postgres 序列 `vc_snapshot_seq` 消耗一个号。**撤销响应与验证响应返回
   同一个快照号**；快照单调递增（事务回滚可能造成空洞，不保证连续）。
5. **密钥轮换保留生效区间**：轮换只追加新的 `issuer_keys` 行，旧行被盖上
   `retired_at`（= 新键生效时刻）。轮换前签发的凭证，无论历史重放还是当前
   验证，都仍能按旧公钥验签；轮换后的凭证由新键签发。
6. **缓存不得在撤销后给出旧结论**，两层保证：
   - 缓存键含全局快照号，任何追加（含撤销）都会让旧条目不可达；
   - “当前时刻”查询的条目还带 `freshUntil`（最近的到期/撤销生效/生效起点），
     纯时间流逝越过边界时条目同样失效；
   - 记录一条未来生效的撤销也会立刻推进快照、立即使旧缓存不可达。
7. **并发撤销只有一个赢家**：撤销事务先 `SELECT … FOR UPDATE` 锁凭证行，再靠
   `revocations(credential_id)` 唯一索引兜底；并发 N 个撤销恰好 1 个成功，其余
   409，输家不消耗快照号。

判定输出（`verdict`）：`VALID | REVOKED | EXPIRED | PURPOSE_MISMATCH |
CONTENT_MISMATCH | INVALID_SIGNATURE | KEY_INACTIVE | UNKNOWN`。

判定顺序：未签发(UNKNOWN) → 撤销(REVOKED) → 有效期(EXPIRED) → 签发时密钥区间
(KEY_INACTIVE) → Ed25519 验签(INVALID_SIGNATURE) → 内容摘要(CONTENT_MISMATCH) →
用途绑定(PURPOSE_MISMATCH) → VALID。

## 目录结构

```
cmd/server/                 HTTP 服务入口
internal/crypto/            canonical JSON、SHA-256 摘要、Ed25519 签名/验签（真实密码学）
internal/clock/             系统时钟与测试用可控时钟
internal/domain/            实体、判定枚举、Store 接口
internal/store/             PostgreSQL 实现（行锁、唯一索引、嵌入式 SQL 迁移）
internal/store/memstore/    同语义内存实现（供无 DB 单元测试；含 TestOnly 变异钩子）
internal/service/           发行/轮换/撤销/重放判定 + 快照缓存
internal/httpapi/           Chi 路由与 JSON 处理
test/integration/           PostgreSQL + HTTP 端到端测试（含并发撤销竞态）
examples/                   示例输入与一键验收脚本
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查，返回当前快照号 |
| GET  | `/v1/snapshot` | 当前全局快照号（空库为 0） |
| POST | `/v1/issuers` | 建合成签发者 + 创世密钥（返回公私钥，**仅测试用途**） |
| GET  | `/v1/issuers/{id}` | 读取签发者 |
| POST | `/v1/issuers/{id}/keys/rotate` | 轮换密钥（body 可选 `{"close_previous": true}`） |
| POST | `/v1/issuers/{id}/keys/retire` | 停用当前密钥（区间关闭） |
| POST | `/v1/credentials` | 发行凭证（真实签名） |
| GET  | `/v1/credentials/{id}` | 读取凭证信封 |
| POST | `/v1/credentials/{id}/revoke` | 追加撤销事件 |
| POST | `/v1/revocations` | 同上（body 带 `credential_id`） |
| POST | `/v1/verify` | 历史重放验证（`as_of` 可选；`content` 可选，做摘要比对） |

凭证签名覆盖的 canonical 载荷：
`{v, kid, issuer_id, subject, purpose, not_before, expires_at, issued_at, content_hash}`，
其中 `content_hash = "sha256:" + hex(sha256(canonicalJSON(content)))`。

> 安全提示：服务端保存私钥并在 API 中返回私钥，仅为让“本地签名演示”自包含；
> 所有响应都带 `warning: SYNTHETIC TEST KEY MATERIAL`。生产环境切勿如此设计。

## 本地启动

需要 Go 1.22+ 与 PostgreSQL 14+（16 已验证）。

### 方式 A：本机 PostgreSQL

```bash
# 1) 建库建角色（按你的环境调整）
sudo -u postgres psql -c "CREATE ROLE vc_test LOGIN PASSWORD 'vc_test' CREATEDB;"
sudo -u postgres psql -c "CREATE DATABASE vc_index OWNER vc_test;"

# 2) 启动（迁移自动执行）
DATABASE_URL='postgres://vc_test:vc_test@localhost:5432/vc_index?sslmode=disable' \
  go run ./cmd/server
# 默认监听 :8090，可用 VCI_ADDR 覆盖
```

### 方式 B：Docker Compose

```bash
docker compose up -d db        # 起 PostgreSQL（容器内 5432，宿主 55432）
export DATABASE_URL='postgres://vc_test:vc_test@localhost:55432/vc_index?sslmode=disable'
go run ./cmd/server
```

## 验收命令

一键脚本式走查（建签发者 → 发行 → 验证 → 轮换前后签名 → 撤销 →
撤销/验证同快照号 → 历史重放 → 并发撤销）：

```bash
# 依赖：curl、jq；默认 http://localhost:8090
make devdb          # 可选：本机 postgres 上建角色/库（需要 sudo -u postgres）
make run &          # 或：go run ./cmd/server
make accept         # examples/walkthrough.sh：完整 curl 走查
```

自动化测试（纯逻辑单测无需数据库；集成测试需要 PostgreSQL）：

```bash
make test           # 全部单元测试（内存存储 + 真实密码学，含 -race）
make test-db        # PostgreSQL 集成 + HTTP 端到端（RUN_DB_TESTS=1，含并发撤销）
make test-all       # 以上全部
make vet            # go vet + gofmt 检查
```

集成测试默认连接
`postgres://vc_test:vc_test@localhost:5432/vc_index_test?sslmode=disable`
（可用 `TEST_DATABASE_URL` 覆盖）；每个用例前会 **TRUNCATE** 该库并把快照序列
重置为“未使用”，请勿指向生产库。

## 示例输入

`examples/`：

- `issue.json` — 发行请求样例；
- `verify.json` / `verify_as_of.json` — 当前验证 / 历史重放请求样例；
- `revoke.json` — 撤销请求样例；
- `walkthrough.sh` — 可直接运行的完整走查脚本（`make accept`）。

## 设计要点说明

- **为什么缓存键含快照号就够安全？** 验证先读全局 head，再读加了 `FOR SHARE`
  锁的凭证/密钥行；撤销事务持 `FOR UPDATE`，与验证读互斥。撤销提交后 head 必然
  增大，旧缓存键（旧 head）再也匹配不到。读到状态后再复核一次 head，若期间有
  追加入库则按新状态重算（最多重试 3 次），保证“撤销响应快照号 == 紧接的
  验证响应快照号”。
- **历史重放为什么不读“当前是否已撤销”？** `replay()` 只在
  `as_of >= revocation.effective_at` 时应用撤销事件。一张今天撤销的凭证，用
  上个月的 `as_of` 去查仍然是 VALID——这正是“不能用当前状态替代过去结论”。
- **密钥轮换为何不使旧签名失效？** 验证读取的是凭证上记录的 `kid` 对应的
  **历史版本行**（含当时公钥与区间），不是“当前 active key”。
