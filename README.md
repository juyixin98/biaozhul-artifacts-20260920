# 多租户文件块配额预留服务（Rust + Axum）

上传前**预留（reserve）**、上传完成**提交（commit）转实占**、放弃则**取消（cancel）释放**；
预留超过 TTL **自动过期（expire）释放**。配额同时限制**字节数**和**对象数**两个维度。
所有状态转换以 append-only 事件日志持久化（每条事件 `fsync`），进程重启后重放恢复。

纯后端 HTTP 服务，无界面、无外部数据库/缓存，仅依赖一个日志文件。

---

## 1. 依赖与环境

- Rust / Cargo（开发与验证使用 **rustc 1.98.1**，edition 2021）
- 无系统级依赖；Rust crate 依赖（见 `Cargo.lock`，已锁定）：
  - `axum 0.7` / `tokio 1`（HTTP 服务与异步运行时）
  - `serde` / `serde_json`（JSON）
  - `async-trait`（自定义提取器）
  - `tower`、`http-body-util`（仅测试中发 in-process 请求）
- 运行时只需要一个可写目录存放事件日志文件（默认 `./data/events.log`）。

## 2. 构建与启动

```bash
# 开发模式
cargo run

# 发布构建后运行
cargo build --release
./target/release/quota-reservation --addr 127.0.0.1:8080 --data ./data/events.log
```

命令行参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--addr <HOST:PORT>` | `127.0.0.1:8080` | 监听地址 |
| `--data <PATH>` | `./data/events.log` | 事件日志文件（不存在则创建） |
| `--clock <system\|injected>` | `system` | 时钟模式。`injected` 时可经管理接口推进时间，用于演示/测试超时 |
| `--clock-start-ms <MS>` | `1700000000000` | injected 模式的初始时钟 |
| `--default-ttl-ms <MS>` | `60000` | 请求未指定 `ttl_ms` 时的默认预留 TTL |
| `--sweep-ms <MS>` | `200` | 后台超时扫描间隔 |

生产用 `--clock system`；服务启动时会先扫描一次（释放宕机期间到期的预留），之后按 `--sweep-ms` 周期扫描，另外每次针对某租户的操作也会**惰性过期**该租户的到期预留。

## 3. 运行测试与示例

```bash
cargo test                 # 9 个集成测试（含并发抢占/超时穿插/持久化恢复）
bash examples/requests.sh  # 需要先启动服务（脚本内含完整 curl 调用序列）
```

---

## 4. HTTP 接口

所有请求/响应均为 JSON。错误响应形如 `{"error":"<code>","detail":"..."}`。

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET  /healthz` | 健康检查，返回时钟模式与当前时间 |
| `POST /tenants` | 创建租户（配额） |
| `GET  /tenants/:tenant_id` | 租户配额与占用视图 |
| `POST /tenants/:tenant_id/reservations` | 创建预留 |
| `GET  /tenants/:tenant_id/reservations/:rid` | 查询预留 |
| `POST /tenants/:tenant_id/reservations/:rid/commit` | 提交：预留转实占 |
| `POST /tenants/:tenant_id/reservations/:rid/cancel` | 取消：释放预留 |
| `POST /admin/clock/advance` | （仅 injected 时钟）推进时钟并立即过期扫描 |
| `POST /admin/expire-due` | 立即执行一次到期扫描（两种时钟模式均可） |

### `POST /tenants`

```json
{ "tenant_id": "acme", "quota_bytes": 1000, "quota_objects": 10 }
```

### `POST /tenants/:tenant_id/reservations`

```json
{
  "size_bytes": 300,          // 必填，>0，本次预留的字节数
  "objects": 3,               // 必填，>0，本次预留的对象（块）数
  "ttl_ms": 60000,            // 可选，>0；缺省用 --default-ttl-ms
  "reservation_id": "up-7788",// 可选，客户端指定 ID（冲突 409）；缺省服务端生成
  "idempotency_key": "k-42"   // 可选，同租户下同 key 的重复请求返回原预留
}
```

成功返回 `201`（幂等重放返回 `200` 且 `created=false`）：

```json
{ "created": true,
  "reservation": {
    "tenant_id": "acme", "reservation_id": "rsv_...",
    "size_bytes": 300, "objects": 3, "status": "held",
    "created_at_ms": 1700000000000, "expires_at_ms": 1700000060000 } }
```

### 租户视图 `GET /tenants/:tenant_id`

```json
{ "tenant_id":"acme", "quota_bytes":1000, "quota_objects":10,
  "committed_bytes":300, "committed_objects":3,
  "held_bytes":100, "held_objects":1, "active_reservations":1 }
```

### 取消响应

`POST .../cancel` → `{ "reservation": {...}, "released": true }`；
对**同一预留重复取消**返回 `200` 但 `"released": false`，不会再次释放额度。

### 预留状态机

```
            reserve
              │
              ▼
           ┌──────┐  commit   ┌───────────┐
           │ held │──────────▶│ committed │（实占，计入 committed_*）
           └──────┘           └───────────┘
             │  │
   cancel ───┘  └──── 到达 expires_at_ms（后台扫描/惰性过期）
      ▼                      ▼
 ┌───────────┐         ┌─────────┐
 │ cancelled │         │ expired │（预留额度已释放）
 └───────────┘         └─────────┘
```

- `held → committed`：成功；重复 commit 幂等；`cancelled/expired` 状态 commit 返回 409/422。
- `held → cancelled`：成功并释放；重复 cancel 返回 `released:false`（不多释放）；committed 状态 cancel 返回 409；expired 状态 cancel 返回 422。
- 过期以**注入时钟**的 `now_ms >= expires_at_ms` 判定。

### 错误码（HTTP 状态）

| 状态码 | error | 触发场景 |
| --- | --- | --- |
| 400 | `bad_request` | JSON 非法/缺字段/未知字段、`size_bytes/objects/ttl_ms` 非法 |
| 400 | `clock_not_controllable` | system 时钟模式调用推进时钟接口 |
| 404 | `not_found` | 租户或预留不存在 |
| 409 | `conflict` | 租户/显式预留 ID 重复；对已提交/已取消预留做非法转换 |
| 422 | `expired` | 提交或取消一个已过期预留 |
| 429 | `quota_exceeded` | 预留将使字节或对象数超限（响应带 limit/used/held/want 明细） |
| 500 | `storage_error` | 事件日志写入/同步失败 |

配额判定：`committed + 活跃held（不含本次） + 本次申请 ≤ limit`，字节与对象数两维同时成立才放行。

---

## 5. 设计要点（对应验收项）

- **并发不超卖**：所有状态转换在单把 `Mutex<Inner>` 临界区内完成「惰性过期 → 查余量 → 扣减 → 追加 fsync 事件」，原子串行。HTTP 层为 `tokio` 多线程运行时，并发请求在锁上排队，不可能共同越过同一剩余额度。
- **超时**：`Clock` trait 抽象，生产用系统墙钟，测试/演示用可推进的注入时钟；后台周期扫描 + 读路径惰性过期双保险。
- **所有状态转换持久化**：仅一个 append-only JSONL 事件日志（`tenant_created/reserved/committed/cancelled/expired`），每条写入后 `flush + fsync`；启动逐行重放重建内存索引。容忍日志末尾因崩溃产生的半行，其余损坏直接报错拒绝启动。
- **重复取消不多释放**：取消是状态机转换，只有当前为 `held` 才释放并写事件；重复取消命中 `cancelled` 分支直接返回 `released:false`。
- **幂等**：预留支持 `idempotency_key`；提交/取消天然幂等。

### 持久化文件格式（示例行）

```json
{"type":"tenant_created","tenant_id":"acme","quota_bytes":1000,"quota_objects":10,"at_ms":1700000000000}
{"type":"reserved","tenant_id":"acme","reservation_id":"rsv_...","size_bytes":300,"objects":3,"expires_at_ms":1700000060000,"idempotency_key":null,"at_ms":1700000000000}
{"type":"committed","tenant_id":"acme","reservation_id":"rsv_...","at_ms":1700000010000}
```

---

## 6. 实际运行结果记录（本机，2026-09-23）

环境：rustc/cargo 1.98.1；Linux 6.8。

- `cargo build --release`：成功。
- `cargo test`：**9/9 通过**（用时约 0.01s）：
  - `reserve_commit_lifecycle`：预留/提交/重复提交/已提交拒绝取消
  - `cancel_releases_and_is_idempotent`：取消释放、重复取消 `released=false` 不多释放
  - `concurrent_reservations_never_oversubscribe`：20 个并发各申请 100B/1 个、配额 1000B/100 个 → 恰好 10 个 201、10 个 429
  - `timeout_interleaved_with_commit_and_cancel`：推进时钟、过期与提交/取消穿插，最终断言 `committed+held ≤ quota` 两维均成立
  - `idempotency_key_single_reservation`：5 个并发同 key 只产生 1 条预留
  - `state_recovers_from_event_log`：复制事件日志模拟新进程重放，committed/held/各预留状态完整恢复并可继续运转
  - `object_count_quota_enforced_separately`、`validation_and_not_found`、`system_clock_rejects_advance`
- 并发用例额外连续重跑 30 次，全部通过，未出现超卖。
- release 二进制真实 HTTP 端到端验证（injected 时钟）：建租户 → 12 个并发预留（10×201 + 2×429）→ 提交/重复提交/取消/重复取消（released true→false）/已提交取消 409 → 推进 60001ms 后恰好 8 条过期 → 过期预留提交/取消均 422 → 释放额度可复用（900B 成功、再多 1B 返回 429 明细）→ 幂等键重放返回同一条 committed 记录（200, created=false）→ 事件日志 22 行 → 杀进程用同一日志重启，状态完全恢复，再推进时钟继续过期并把日志追加到 23 行。

## 7. 未完成项 / 已知限制（如实说明）

- 事件日志**不做压缩/快照/分卷**：长期运行文件会无限增长；重放在启动时一次性完成。生产化需要周期性 snapshot + 日志轮转。
- 单进程、单把全局锁：所有租户的写操作串行。当前面向正确性而非吞吐；多租户高并发下可拆成「每租户一把锁」。
- 没有鉴权/租户隔离认证、没有限流（依赖调用方信任）；管理接口 `/admin/*` 无令牌保护，仅建议绑定内网/本机。
- 时间用 `i64` 毫秒，时钟不可回拨（injected 时钟 `set` 只接受不小于当前值的时刻）。
- 未做 HTTP 客户端重试退避示例与压测基准（bench）；并发正确性以集成测试 + 重复 30 次验证，未做形式化证明。
