# 中继租约与序号协调（relay-coord）

为本地模拟目标链实现的**多中继提交协调服务**。纯后端，Rust + Axum + PostgreSQL，无前端。

三个进程，全部真实实现、真实通信（HTTP+JSON / PostgreSQL 事务）：

| 进程 | 二进制 | 职责 |
|---|---|---|
| 协调器 | `coordinator` | 稳定提交 ID、租约、fencing token、每通道 nonce、回执裁决（PostgreSQL 持久化） |
| 目标桩 | `target-stub` | 模拟目标链：幂等提交、结果查询、真实 SHA-256 交易哈希、故障注入 |
| 中继 | `worker` | 领租约 → 提交 → 超时先查询再重发 → 回执（可多进程并发） |

## 核心语义（与需求逐条对应）

1. **稳定提交 ID**：每条消息在投递时生成一次 `submission_id`（UUIDv4），此后所有提交、重发、查询都用它，绝不重新生成。
2. **递增 fencing token**：每次领取租约，协调器在事务内把全局计数器 `+1` 写入该消息。旧代中继持旧 token 的回执被裁决为 `stale_token` 拒绝，**状态绝不回退**。
3. **超时可重领**：租约带 TTL；到期未回执的消息重新变为可领取，由新一代（更大 token）接管。
4. **网络超时先查询再重发**：worker 提交超时/连不通只代表"结果未知"——先 `GET /result/{id}` 查询；查到终态按终态走，查不到/查不通才用**同一个 ID** 重发。绝不把未知当失败。
5. **每通道 nonce 顺序**：消息在通道内拿连续 nonce；协调器只放出"队头"（前面无未完成消息）的租约，目标桩按 nonce 严格上链。
6. **失败阻塞后续但不阻塞其他通道**：确定性失败 → 消息 `failed` + 通道 `blocked`，该通道后续全部暂停；其他通道不受影响。`POST /v1/messages/{id}/retry` 显式重试后解锁。

## 构建

```bash
cargo build            # 生成 coordinator / target-stub / worker 三个二进制
```

依赖已锁定（`Cargo.lock` 已提交）。

## 本地启动

需要本地 PostgreSQL（开发默认 `admin` / `relay_coord_dev`，见 `.cargo/config.toml`）：

```bash
# 1. 建库（首次）
PGPASSWORD=relay_coord_dev psql -h 127.0.0.1 -U admin -d postgres -c "CREATE DATABASE relay_coord;"

# 2. 启动协调器（自动跑迁移）
DATABASE_URL="postgres://admin:relay_coord_dev@localhost/relay_coord" \
  ./target/debug/coordinator --listen 127.0.0.1:8080

# 3. 启动目标桩
./target/debug/target-stub --listen 127.0.0.1:9090

# 4. 启动一个或多个中继（可开多个进程，跨进程竞争安全）
./target/debug/worker --coordinator http://127.0.0.1:8080 --target http://127.0.0.1:9090 \
  --relay-id relay-1 --lease-ttl 10 --submit-timeout 2
```

投递消息（示例输入在 `examples/`）：

```bash
curl -X POST http://127.0.0.1:8080/v1/messages \
  -H 'content-type: application/json' \
  -d @<(jq -c '.channel_id="payments"' examples/enqueue_payment.json)
```

## 一键验收

```bash
# 端到端演示：真实起三个进程，覆盖全部 7 个场景（丢响应/顺序/竞争/阻塞/恢复/fencing/未知≠失败）
./scripts/demo.sh

# 自动化测试（真实 PostgreSQL + 真实多进程）
cargo test
```

`cargo test` 覆盖：

| 测试 | 场景 |
|---|---|
| `lost_response.rs` | 提交成功但响应丢失 → 查询恢复；commit 窗口内查询 404 → 同 ID 重发；目标观测到的 fencing token 与租约一致 |
| `lease_fencing.rs` | 租约过期重领、token 严格递增、旧代回执 `stale_token`/`already_final` 被拒、状态不被覆盖 |
| `cross_process.rs` | **三个真实 OS worker 进程**竞争：每条消息恰好上链一次、nonce 连续无空洞、tx 哈希真实对应负载、同通道确认有序 |
| `channel_order.rs` | 队头失败阻塞本通道、不阻塞其他通道、blocked 通道拒绝新投递、retry 恢复 |
| `recovery.rs` | 协调器进程宕机重启后，孤儿租约过期被新 worker 接管确认 |
| `lib.rs` 单元测试 | 退避序列、状态机 |

## HTTP 接口

### 协调器 `:8080`

```
POST /v1/messages                      投递 {channel_id, payload} → 分配 nonce + submission_id
GET  /v1/messages/{id}                 查询消息状态
POST /v1/messages/{id}/retry           failed 消息重试，解除通道阻塞
POST /v1/leases/acquire?relay_id=…&ttl_secs=…   领取队头租约（204=暂无）
POST /v1/leases/{id}/receipt           回执 {fencing_token, tx_id|error}
GET  /v1/channels                      通道列表（blocked / next_nonce）
GET  /healthz
```

回执裁决：`applied`（token 匹配，已落盘）/ `stale_token`（旧代，拒绝）/ `already_final`（已终态，幂等忽略）。

### 目标桩 `:9090`

```
POST /submit                  提交 {submission_id, channel_id, nonce, fencing_token, relay_id, payload}
                              幂等：重复提交返回同一记录（replayed=true）
GET  /result/{id}             按提交 ID 查询结果（404=未知，不是失败）
GET  /records                 全部链上记录（审计）
PUT  /admin/rules/{id}        故障注入 {drop_responses, sleep_secs, commit_delay_secs, query_delay_secs, always_fail}
DELETE /admin/rules/{id}      清除故障规则
GET  /healthz
```

交易 ID = `0x + sha256(submission_id | channel | nonce | payload)`，真实计算。

## 故障注入（目标桩）

| 规则字段 | 效果 | 模拟的真实情况 |
|---|---|---|
| `drop_responses: N` | 前 N 次提交已落盘但响应拖住 | 提交成功但响应在网络丢失 |
| `sleep_secs` | 拖住响应的秒数 | 网络延迟 |
| `commit_delay_secs` | 接受后延迟落盘（期间查询 404） | 链上确认慢，结果真正未知 |
| `query_delay_secs` | 结果查询也延迟 | 对 GET 也丢包 |
| `always_fail` | 确定性拒绝（422） | 链上业务拒绝 |

## 设计要点

- **fencing 在数据库事务内裁决**：`submit_receipt` 先 `SELECT ... FOR UPDATE` 读当前 token，不匹配直接回滚返回 `stale_token`，绝不写。多协调器进程、多 worker 进程并发都安全。
- **领取用 `FOR UPDATE SKIP LOCKED`**：同一时刻一条消息只被一个中继领走；被锁的行直接跳过，不阻塞。
- **worker 的提交超时被钳制到租约剩余时间内**：租约快到期就不再发起新提交，避免旧代在过期后收到迟到响应抢写状态（fencing 是最后防线，这是第一道）。
- **目标桩的延迟落盘在独立 tokio 任务里**：客户端超时断开不影响"已接受的交易最终上链"（真实 mempool 语义）。

## 目录

```
migrations/0001_init.sql   建表：channels / fencing_seq / messages
src/db.rs                  PostgreSQL 事务层（enqueue/acquire/receipt/retry）
src/coordinator.rs         协调器 HTTP 接口
src/target.rs              目标桩（幂等提交/查询/故障注入/真实哈希）
src/worker.rs              中继循环（查询-重发协议、租约预算、fencing 回执）
src/client.rs              共用 HTTP 客户端
src/bin/                   三个可执行文件
tests/                     集成测试（真实 PG + 真实多进程）
examples/                  示例输入 JSON
scripts/demo.sh            端到端验收演示
```
