# ximbox — 跨链消息收件箱（Go + Chi + PostgreSQL）

纯后端实现：两条**模拟源链**（`chainA` / `chainB`）向收件箱投递经过**真实
Ed25519 签名**的跨链消息。服务端只信任本地夹具的公钥，对每条消息执行真实的
SHA-256 摘要校验和 Ed25519 验签；执行阶段对 PostgreSQL 账户表做真实的余额
计算，且与消息状态翻转处于**同一事务**。

没有前端页面，只有 JSON HTTP API。

---

## 1. 消息模型与不变量

消息键 = `(source_chain, channel, sequence)`。

签名文档（规范 JSON，键序固定）：

```json
{
  "source_chain": "chainA",
  "channel": "orders",
  "sequence": 0,
  "block_hash": "0x..",          // 消息锚定的源块（也被签名绑定）
  "body_digest": "0x.."          // 正文原始字节的 SHA-256
}
```

正文 `body` 以**规范化紧凑 JSON**（去空白、数字字面量用 `UseNumber`
精确保留）计算 SHA-256，摘要进入签名文档：篡改语义内容必然改变摘要；即使
重算摘要也会在 Ed25519 验签处失败。签名端与验签端做同一规范化，因此信封
经过 JSON 序列化/反序列化传输（Go 的嵌套 RawMessage 会被重编码为紧凑形式）
后依然可验。

服务端保证的不变量：

1. **正文不可变**：同一键下已有记录，新的副本若 `body_digest` 相同，视为
   无害重放；若不同，就是**冲突证据（equivocation）**。
2. **只有锚定到 `final` 块的消息可执行**；锚定在 `proposed` 块的消息保持
   `pending`。
3. **同序号只执行一次**：执行 = 一个数据库事务，同时完成余额写入、消息置
   `executed`、写执行台账 `executions`。重放同一信封不会二次入账。
4. **乱序暂存、按连续前缀推进**：执行器始终从“下一个期望序号”开始，遇
   断档暂停；断档补齐后自动继续。
5. **确认前撤销源块（重组）**：在同一事务中把高度 ≥ 分叉点的存活块置
   `revoked`，并把锚定其上的 `pending` 消息置 `cancelled`；已 final 的
   高度拒绝被另一哈希覆盖（返回 409，不悄悄重写）。
6. **已执行消息出现冲突副本**：冲突副本原样写入 `message_evidence`，
   **绝不覆盖**原消息；通道置 `frozen`（粘性），写一条
   `equivocation_executed` 告警，后续投递返回 409。冻结后需要人工处置。
7. **执行前崩溃**：崩溃点设在“SQL 全部成功、事务尚未提交”的瞬间；事务
   回滚，重启时 `RecoverOnStartup` 重新推进前缀，每个序号恰好生效一次。

消息/账户状态可通过 API 直接观察；告警与证据也可查询。

---

## 2. 目录结构

```
cmd/server/          HTTP 服务入口（含启动恢复、崩溃钩子开关）
cmd/signfixture/     本地源链/中继模拟器：派生确定性 Ed25519 密钥、签名
internal/cryptoenvelope/  SHA-256 摘要、规范签名文档、Ed25519 签发/验签、夹具
internal/store/      pgx 数据访问 + 内嵌 SQL 迁移（0001_init.sql）
internal/inbox/      核心状态机：区块重组/最终性、候选、执行、冻结、告警、崩溃恢复
internal/server/     chi 路由与 HTTP 错误映射
internal/config/     环境变量配置
examples/            真实签名的示例输入与两个端到端演示脚本
tests/               密码学单测、服务级集成测试、HTTP API 测试、真实二进制崩溃/重启 e2e
```

---

## 3. 本地启动

### 3.1 前置

- Go ≥ 1.22（在 1.22.2 上验证；pgx 锁定 v5.7.1，无需 1.25）
- PostgreSQL ≥ 14（在 16 上验证）

### 3.2 建库（Debian/Ubuntu 默认 peer 认证）

```bash
make db-create
```

等价 SQL（角色/库已存在时安全重复执行）：

```sql
CREATE ROLE ximbox LOGIN PASSWORD 'ximbox_dev_pwd';   -- 不存在时
CREATE DATABASE ximbox      OWNER ximbox;
CREATE DATABASE ximbox_test OWNER ximbox;
```

连接串默认：

```
postgres://ximbox:ximbox_dev_pwd@localhost:5432/ximbox?sslmode=disable
```

可用环境变量覆盖：`XIMBOX_DATABASE_URL`、`XIMBOX_HTTP_ADDR`（默认 `:8080`）。

### 3.3 构建并运行

```bash
make build          # 产出 bin/ximbox-server 与 bin/ximbox-signfixture
make run            # 自动迁移、注册两条链、执行启动恢复、监听 :8080
```

健康检查：

```bash
curl -s http://127.0.0.1:8080/healthz
```

> 若本机 8080 已被占用，用 `XIMBOX_HTTP_ADDR=127.0.0.1:18080 make run`
> 换端口；演示脚本可用 `XIMBOX_BASE=http://127.0.0.1:18080 make demo`、
> `XIMBOX_PORT=18080 make demo-crash`。

查看受信公钥（确定性夹具，每台机器相同；仅用于本地测试，密钥公开可知）：

```bash
./bin/ximbox-signfixture keys
```

---

## 4. HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/blocks` | 提议源块 `{source_chain,height,hash,parent_hash}`；同高度异哈希触发重组 |
| POST | `/v1/blocks/confirm` | `{source_chain,hash}`，级联确认祖先块，随后推进可执行前缀 |
| GET  | `/v1/blocks?source_chain=` | 列区块（含 `proposed/final/revoked`） |
| POST | `/v1/messages` | 投递**已签名**信封（结构见下） |
| GET  | `/v1/messages?source_chain=&channel=` | 列消息及状态 |
| POST | `/v1/channels/advance` | 手动触发某通道前缀执行 |
| GET  | `/v1/channels/{chain}/{channel}` | 通道状态（active/frozen） |
| GET  | `/v1/alerts?source_chain=&channel=` | 告警（equivocation/execution_failed） |
| GET  | `/v1/evidence?source_chain=&channel=` | 冲突证据副本 |
| GET  | `/v1/accounts` | 执行产生的应用状态（余额） |

消息信封（`POST /v1/messages`）：

```json
{
  "source_chain": "chainA",
  "channel": "orders",
  "sequence": 0,
  "block_hash": "0x....",
  "body": {"type": "transfer", "to": "alice", "amount": 100},
  "body_digest": "0x....",
  "signer": "0x....",
  "signature": "0x...."
}
```

应用目前只真实执行一种指令：`{"type":"transfer","to":"<addr>","amount":<正整数>}`，
效果是给目标账户加余额。非法指令会把该消息置为 `execute_failed` 并告警，
其后序号不能跳过它（前缀被卡住，强制人工介入）。

错误码：`400` 请求/签名格式或父块问题；`401` 验签失败；`404` 块或通道不存在；
`409` 通道冻结 / 已 final 高度冲突 / 块已撤销。

---

## 5. 快速演示（验收命令）

### 5.1 正常路径 + 重放 + 冲突冻结

```bash
make build
make run                 # 终端 A
make demo                # 终端 B
```

`examples/demo_walkthrough.sh` 用**夹具真实签名**逐步演示：

1. 提议并确认区块；
2. 先投 seq1（块未确认→暂存），再投 seq0（块已确认→立即执行）；
3. 确认块 2 后暂存的 seq1 执行，余额 `alice=100,bob=25`；
4. 重放 seq0 两次，余额不变（恰好一次）；
5. 投递 seq0 的**冲突副本**（不同正文、签名合法）：返回
   `channel_frozen:true`，`/v1/evidence` 保留冲突副本，`/v1/alerts` 有
   `equivocation_executed` 告警，`mallory` 余额始终为 0。

### 5.2 重组：确认前撤销源块、取消候选

由自动化测试 `TestReorgBeforeConfirmCancelsCandidates` 完整覆盖：

```bash
go test ./tests/ -run TestReorgBeforeConfirmCancelsCandidates -v
```

过程：块 2 `proposed` 上的 seq1 为 `pending` → 在高度 2 提议竞争块（重组）
→ seq1 变 `cancelled`，高度 1 已执行的 seq0 不受影响 → 用新块重新锚定并
签名 seq1 → 确认新链后 seq1 正常执行。

### 5.3 执行前崩溃与重启（真实进程）

```bash
make demo-crash
```

脚本：

1. 先启动一个准备进程，写入已确认链、seq0 已执行、seq1 已暂存，并把块 2
   直接置 final（模拟“已确认但尚未执行”的时刻）；
2. 用 `XIMBOX_CRASH_AT=source_chain=chainA,channel=crash-demo,sequence=1`
   启动服务器 —— 启动恢复时执行到 seq1，在事务提交前 `exit(99)`；
3. 打印进程退出码（99）、日志中的 `CRASH INJECTION`，并直接查询数据库证明
   seq1 事务已整体回滚（无台账、无余额、消息仍 `pending`）；
4. 不带钩子重启：启动恢复把 seq1 恰好执行一次；再次重启余额不变。

崩溃钩子是测试专用代码路径（`internal/inbox/crashhook.go`），仅在设置了
`XIMBOX_CRASH_AT` 时生效。

---

## 6. 自动化测试（验收命令）

```bash
# 需要 PostgreSQL；默认连 ximbox_test，可用 XIMBOX_TEST_DATABASE_URL 覆盖
make test          # go test ./...
make test-race     # -race 全量
```

覆盖内容：

| 测试 | 覆盖点 |
| --- | --- |
| `internal/cryptoenvelope` 单测 | 真实签名/验签往返；篡改正文/序号/通道/链/锚点/签名全部拒绝；错误受信密钥拒绝；夹具确定性 |
| `TestGapFill` | 乱序暂存、确认后连续前缀推进、真实余额计算 |
| `TestDuplicateExecutesOnce` | 同序号同摘要多次到达只执行一次（台账+余额） |
| `TestConflictingCopyFreezesChannel` | 已执行消息遇冲突副本：不覆盖、留证据、冻结、告警、拒绝后续投递 |
| `TestReorgBeforeConfirmCancelsCandidates` | 确认前撤销源块→候选取消；新链重新锚定后执行 |
| `TestFinalBlockConflictRefused` | 已 final 高度不可被异哈希覆盖 |
| `TestUnconfirmedNotExecutable` | 未确认块上的消息绝不执行 |
| `TestInvalidSignatureRejected` | 翻转签名字节→401 |
| `TestInvalidBodyHaltsPrefix` | 非法指令失败并卡住后续序号、写告警 |
| `TestCrashBeforeCommitAndRestart` | **真实二进制**：提交前 exit(99)→回滚→重启恢复恰好一次→再重启幂等 |
| HTTP API 测试 | 健康检查、404、错误码映射、证据/告警/账户查询 |

若测试数据库不可达，测试会 **skip 并打印原因**，不会假装通过。

---

## 7. 密码学说明

- 签名算法：标准库 `crypto/ed25519`（Ed25519，RFC 8032），无任何自造签名。
- 摘要：标准库 `crypto/sha256`（SHA-256），对正文的**规范化紧凑 JSON**
  字节计算（`json.Decoder.UseNumber()` 保留整数字面量精度）。
- 编码：十六进制统一 `0x` 前缀；签名文档为紧凑规范 JSON（固定字段顺序）。
- 夹具：私钥由 `SHA-256("ximbox/local-fixture-key/v1:"+chain)` 作为
  Ed25519 seed 确定性派生，因此所有环境得到相同密钥对、示例可复现验证。
  这些密钥**公开可知，仅供本地测试**，切勿用于真实网络。
- 信任锚：服务端启动时从固定注册表读取 chainA/chainB 公钥；信封里的
  `signer` 必须等于该链受信公钥，否则拒绝。

---

## 8. 依赖锁定

见 `go.mod` / `go.sum`：

- `github.com/go-chi/chi/v5 v5.1.0`
- `github.com/jackc/pgx/v5 v5.7.1`（及其 pgpassfile/pgservicefile/puddle 等传递依赖）
- 其余仅用 Go 标准库

`go.sum` 已提交，`go build`/`go test` 使用校验后的固定版本。

---

## 9. 设计取舍

- **执行粒度**：每条消息一个事务，避免长事务持锁，也让“崩溃时最多丢一个
  未提交事务”，重启即可续跑；余额、消息状态、执行台账同事务保证原子性。
- **执行失败不跳过**：失败消息不推进“下一期望序号”，防止坏消息被静默绕过；
  通道其余部分显式告警。这是“不能悄悄回写”在失败场景下的对应策略。
- **重组边界**：只允许撤销未 final 的块；final 冲突直接报错并保留现场。
- **冻结粘性**：通道一旦因证据冲突冻结不会自动恢复，所有新投递被拒绝，
  逼迫人工核查证据，符合跨链桥安全惯例。
