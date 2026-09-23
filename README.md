# 中继租约与序号协调（relay-coord）

为本地模拟目标链实现的**多中继提交协调服务**（纯后端，Rust + Axum + PostgreSQL）。
多个中继进程从同一队列领取提交并上链，协调器保证：

- **稳定提交 ID**：消息 ID 由外部给定且全局唯一；超时重发沿用同一 ID，绝不新造 ID。
- **Fencing token（租约代次）**：每次领取 `fence += 1`；所有回执必须携带 `(id, fence, relay_id)`，
  旧代次中继的迟到回执命中 0 行 → `409`，**不可能覆盖新代次状态**。
- **超时可重领**：租约到期后任何中继可重领；`nonce` 在首次领取时分配并重领复用，不跳号。
- **先查后发**：对目标的请求超时/5xx 时，先按提交 ID 调 `GET /receipts`；
  已入账则收敛确认，明确 `404 未知` 才用**同一 ID** 重发——绝不把未知当失败造新 ID。
- **按通道 nonce 顺序**：每通道 nonce 严格连续；队头失败（fatal）阻塞本通道后续，
  但**不阻塞其他通道**。
- **真实密码学**：协调器↔目标的请求用 HMAC-SHA256 签名（常量时间校验、时间戳防重放），
  目标交易哈希为域分离 SHA-256。无任何占位/假实现。

## 架构

```
                        ┌──────────── 协调器 (Axum, :18080) ───────────┐
 enqueue ──HTTP────────▶│  channels / submissions / delivery_attempts │◀──HTTP── 中继 relay-A
  (客户端)               │  领取事务：行锁 + fence+1 + nonce 分配        │           relay-B  …
                        └───────────────────────▲──────────────────────┘            │
                                   内部回执 API（fence 校验，旧代次 409）            │ HMAC 签名
                                                                                    ▼
                                                              ┌── 目标链桩 (Axum, :19090) ──┐
                                                              │ 幂等入账 / nonce 连续校验     │
                                                              │ GET /receipts 按 ID 查询      │
                                                              │ 故障注入：丢响应/5xx/422/延迟 │
                                                              └──────────────────────────────┘
```

- `migrations/0001_init.sql`：全部不变量落在数据库约束与事务里。
- `src/db.rs`：领取事务（`FOR UPDATE SKIP LOCKED` 跨进程竞争）、fence 条件更新。
- `src/stub.rs`：目标链桩，真实 HMAC 验签、nonce 间隙检测、幂等与故障注入。
- `src/worker.rs`：中继领取→心跳→提交→（超时）查询→收敛→fenced 回执。
- `src/crypto.rs`：HMAC-SHA256 / SHA-256 真实实现。
- `tests/e2e.rs`：11 个真实进程 + 真实 Postgres 的端到端测试。

### 提交状态机

```
pending ──claim(fence+1, 分配 nonce)──▶ leased ──目标受理──▶ delivered ──回执(tx_hash)──▶ confirmed
  ▲            │ 租约到期/被重领(fence 再+1)      │              │
  └─retryable──┴──────────────────────────────────┴──────────────┘
               fatal ──▶ failed（阻塞本通道队头；POST /submissions/{id}/requeue 解除）
任何带旧 fence 的写操作 → 409，状态不变。
```

## 环境要求

- Rust（在 1.98 上构建验证；`cargo` 会按 Cargo.lock 锁定版本）
- PostgreSQL（在 16 上验证），本机可通过 Unix socket 或 `DATABASE_URL` 连接，
  账号需有建库/建表权限。

## 本地启动（一键演示）

```bash
./scripts/run-demo.sh           # 建库 relay_coord_demo + 迁移 + 起桩/协调器/2 个中继
curl -s -X POST http://127.0.0.1:18080/enqueue \
  -H 'content-type: application/json' --data @examples/enqueue.json | jq
curl -s http://127.0.0.1:18080/channels/channel-demo/submissions | jq
./scripts/stop-demo.sh
```

也可以手动起四个进程（单二进制四个子命令）：

```bash
createdb relay_coord
DATABASE_URL=postgresql:///relay_coord cargo run -- migrate
cargo run -- stub    --bind 127.0.0.1:19090 --secret dev-shared-secret --mode normal
DATABASE_URL=postgresql:///relay_coord cargo run -- serve --bind 127.0.0.1:18080 --lease-secs 10
cargo run -- relay --relay-id relay-A   # 再起一个 relay-B 即多中继竞争
```

端口说明：本机 8080 常被占用，演示脚本使用 **18080（协调器）/ 19090（桩）**，
均可通过命令行参数或环境变量（`BIND_ADDR`、`STUB_BIND`）覆盖。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/enqueue` | `{channel_id, commits:[{id,payload}]}`，幂等入队，按序占 seq |
| GET  | `/submissions/{id}` | 按稳定提交 ID 查协调器状态 |
| GET  | `/channels/{id}/submissions?limit=` | 通道内按 seq 列出 |
| POST | `/submissions/{id}/requeue` | 解除 failed 阻塞（fence 再 +1，旧在途回执失效） |
| POST | `/internal/claim` | 中继领取；200 返回 `{id,nonce,fence,payload,leased_until_unix}`，无活 204 |
| POST | `/internal/heartbeat` | 续租，fence 不符 409 |
| POST | `/internal/delivered` | 目标已受理（幂等），fence 不符 409 |
| POST | `/internal/complete` | `{id,fence,relay_id,tx_hash}` 最终确认，旧 fence 409 |
| POST | `/internal/report` | `kind=retryable`（交还重领）/ `fatal`（阻塞通道） |
| GET  | 桩 `/submit` / `/receipts` | HMAC 头 `X-Signature`；`/receipts` 未知 ID 返回 **404** |

## 故障注入（验证容错）

桩的 `--mode` 或单条消息 `payload._sim` 支持：
`normal` / `drop_first:<ms>`（入账后延迟响应，模拟成功丢包）/
`fail_attempts:<n>`（前 n 次 503）/ `fatal` / `fatal_once` / `slow_ok:<ms>`。

手工验收“提交成功但响应丢失”的恢复（目标恰好一笔、同 ID/同 nonce、最终 confirmed）：

```bash
cargo run -- stub --bind 127.0.0.1:19090 --secret s --mode normal &
DATABASE_URL=postgresql:///relay_coord cargo run -- serve --bind 127.0.0.1:18080 --lease-secs 10 &
cargo run -- relay &
curl -s -X POST http://127.0.0.1:18080/enqueue -H 'content-type: application/json' -d '{
  "channel_id":"c1","commits":[{"id":"k1","payload":{"_sim":"drop_first:5000"}}]}'
# 数秒后：GET /submissions/k1 -> status=confirmed；GET 桩 /admin/entries 恰好 1 笔
```

## 验收命令

```bash
# 全量测试：2 个密码学单元测试 + 11 个端到端测试。
# 每个用例自动 CREATE/DROP 独立数据库（需连接账号具有 CREATEDB 权限）、
# 独立桩/协调器进程（端口由 OS 分配），可安全并行，约 4~5 秒完成。
cargo test

# 也可串行：
cargo test -- --test-threads=1
```

测试覆盖的场景（对应需求）：

| 测试 | 验证点 |
| --- | --- |
| t1 | 成功路径，nonce 0..n 连续，目标每笔一次 |
| t2 | **提交成功但响应丢失**：超时→查 receipts→收敛，目标仅一笔 |
| t3 | 瞬时失败重试：同 ID、同 nonce、不重复入账 |
| t4 | **租约过期 + 跨进程接管**：旧中继被强杀，新中继 fence≥2 接管，nonce 复用 |
| t5 | **fencing**：旧 fence 的心跳/delivered/complete 全 409，状态不被覆盖 |
| t6 | fatal 阻塞本通道后续、**不阻塞其他通道**；requeue 解除 |
| t7 | 三中继并发：无重复领取、严格 nonce 序 |
| t8 | **协调器重启后恢复**（状态持久化于 PG） |
| t9 | 未知提交 ID → 桩返回 404，不能当失败 |
| t10 | 20 并发 claim 抢 1 条：恰好 1 个赢家 |
| t11 | 桩真实拒绝 nonce 跳号（422）与伪造签名（401） |

## 设计要点

- **竞争安全**：领取在单事务内锁通道行 + 队头行（`FOR UPDATE SKIP LOCKED`），
  并发中继要么拿到不同通道，要么在队头排队；`UPDATE ... WHERE id=$ AND fence=$`
  使旧代次写操作天然失效。
- **nonce 不浪费**：nonce 只在“真正领取队头”时分配；阻塞在失败队头之后的消息拿不到 nonce，
  因此解除阻塞后序列依然连续。
- **租约即代次**：续租、受理、确认、失败上报全部要求 `(fence, relay_id)` 双匹配；
  中继心跳收到 409 即主动中止，杜绝“僵尸中继”晚到覆盖。
