# idemresp —— 幂等响应保存（Idempotent Response Storage）纯后端示例

一个**只依赖 Go 标准库**的本地 HTTP 服务，演示如何为有副作用的请求实现
**幂等层**：以客户端提供的 `Idempotency-Key` 为索引、把键绑定到**请求摘要**，
并让「业务结果 + 待返回给客户端的响应 + 本地副作用」在**同一个本地事务**里
原子提交。网络重试、并发重复、客户端断线、进程硬崩溃都不会让本地事务副作用
执行超过一次。

同时包含一个**本进程内的假外部支付服务（fake gateway）**和一个**故障注入
HTTP 客户端**，所有外部依赖都在本机进程内，可注入超时/断连/崩溃，**不连接
任何生产系统**。

---

## 1. 它要回答的问题

对 `POST /v1/orders`（下单并向支付网关扣款）这样的有副作用请求：

- 客户端重试 / 超时重发 → 不能扣两次款、不能建两张单；
- 同一把幂等键却配了不同请求体 → 必须明确 **409 冲突**，不能静默复用；
- 第一个请求还在处理中，重复请求到达 → 必须明确返回 **409 处理中**，
  而不是再执行一遍；处理完成后的重试则回放同一响应；
- 客户端在服务端提交前 / 提交后断线 → 本地事务副作用仍只发生一次，
  重试拿到同一份已保存响应；
- 进程在提交前 / 提交后硬崩溃并重启 → 已提交的不丢、未提交的不产生
  半提交副作用；
- **诚实的边界**：本地幂等事务**不能**保证「任意外部调用恰好一次」。
  若外部调用结果未知（超时/断连）且没有贯穿到下游的端到端幂等键，
  外部系统可能已经扣款而本地无从得知。本项目用专门的测试演示这一点。

---

## 2. 快速开始

需要 Go 1.22+（仅标准库，无需联网下载依赖）。

```bash
go run ./cmd/idemresp -addr :18080 -gateway-addr :18081 -data-dir ./data
# 另开一个终端
bash examples/requests.sh
# 或交互式逐条执行（VS Code REST Client / JetBrains HTTP Client 可直接打开）
#   examples/requests.http
```

也可以用一键手动验收脚本（固定端口见脚本内）：

```bash
go build -o /tmp/idemresp ./cmd/idemresp
bash scripts/manual-demo.sh http://127.0.0.1:18080 http://127.0.0.1:18081
```

健康检查：`GET /healthz` → `ok`。

### 命令行参数

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `-addr` | `:18080` | 应用 HTTP 监听地址 |
| `-gateway-addr` | `:18081` | 进程内假网关监听地址 |
| `-gateway-url` | 空 | 指向**外部**假网关时使用（崩溃恢复演示中让网关跨进程存活）；设置后不再在本进程启动假网关 |
| `-data-dir` | `./data` | 本地 WAL 数据目录 |
| `-lease` | `30s` | 崩溃遗留 `processing` 占位的租约，过期后同键可被重新接管 |
| `-gateway-timeout` | `2s` | 每次网关调用的 HTTP 超时 |
| `-allow-crash` | `false` | 是否允许 `X-Crash` 触发真实 `os.Exit`（仅测试） |

---

## 3. HTTP 接口

### `POST /v1/orders`

请求头：

- `Idempotency-Key`（**必填**）：每个逻辑请求一个稳定键（如 UUID），
  **重试时复用同一个**，而不是每次 HTTP 尝试换新键。
- `X-Forward-Key`（可选，默认 `true`）：是否把键作为端到端幂等键转发给网关。
- 故障注入（仅假网关/测试用）：`X-Fault`、`X-Pre-Commit-Delay`、
  `X-Post-Commit-Delay`，以及服务以 `-allow-crash` 启动时的 `X-Crash`。

请求体：JSON `{"amount": <正整数>, "currency": "<币种>", "reference": "..."}`。

响应：

| 场景 | 状态码 | 特征 |
| --- | --- | --- |
| 首次执行成功 | `201` | 无 replay 头 |
| 同键同体重试（完成后） | `201` | 头 `Idempotent-Replay: true`，**响应体逐字节相同** |
| 同键不同体 | `409` | `error=idempotency_key_conflict` |
| 处理中重复 | `409` | `error=request_in_progress`，带 `Retry-After` |
| 网关明确失败（4xx/5xx） | `502` | `error=upstream_failure`，键置为 failed，可立即重试 |
| 网关结果未知且未转发端到端键 | `503` | `error=ambiguous_outcome`，不提交本地副作用、不盲目重试 |
| 缺少 `Idempotency-Key` / 非法 JSON / 金额非法 | `400` | 校验失败 |

### 观测接口（验收与排障）

- `GET /v1/keys/<key>` —— 幂等记录（状态、请求摘要、尝试次数、时间戳）。
- `GET /v1/ledger?key=<key>` —— 该键已提交的副作用（记账条目），**`count` 即验收计数**。
- `GET /v1/orders?id=<orderID>` —— 已提交订单。
- 假网关：`GET /gateway/metrics`（真实扣款笔数，外部真相）、`POST /gateway/reset`。

样例报文见 `examples/`（`response-*.json`）。

---

## 4. 核心协议（幂等状态机）

幂等记录在本地有三态：`processing → completed`（终态），或
`processing → failed →（重试）processing → completed`。

```
                 Acquire(key, 请求摘要)
                          │
        ┌─────────────────┼──────────────────────────┐
        ▼                 ▼                          ▼
   未知该键          已 completed               同键不同摘要
  写 processing      回放保存的响应               409 conflict
  占位(WAL,fsync)     201 + Replay:true
        │
        ▼
  执行业务（调用网关，调用结果三分类：成功 / 明确失败 / 未知）
        │
   ┌────┴───────────────┬──────────────────────────┐
   ▼                    ▼                          ▼
 成功               明确失败                    结果未知(超时/断连)
 单一 WAL 记录原子写   置 failed，可立即重试        转发了端到端键→用同键安全重试(网归去重)
 completed 行+订单    （此前无本地副作用）          未转发→503，不提交/不盲试，待对账
 +副作用+响应 ────────┘
```

关键设计：

1. **先占坑后干活**。`processing` 占位在执行业务**之前**就落 WAL 并 fsync。
   并发重复在占坑阶段被串行化，只有一个请求能成为执行者，其余拿到
   「处理中」。这样占位在崩溃后可恢复，不会因为「执行了但没记录」而放行
   第二次执行。

2. **键绑定请求摘要**。摘要 = `SHA256(method "\n" path "\n" 规范化JSON(body))`。
   JSON 的键顺序与空白差异会被规范化（`{"a":1,"b":2}` 与
   `{"b":2,"a":1}` 视为同一请求）；不同载荷产生不同摘要 → 409。

3. **结果与副作用同一本地事务提交**。`completed` 记录（**含要回放的完整
   响应体**）、订单行、记账副作用行作为**同一条 WAL 记录**写入并 fsync，
   然后一次性进入内存状态。恢复时要么整条可见、要么整条不可见，不存在
   「副作用已提交、响应没保存」的半状态。

4. **完成后回放的是保存的响应**，不重新执行业务、不再次调用网关。

### 对「结果未知」的策略（刻意且可演示）

- **转发端到端键**（默认）：未知后用**同一个键**再问一次网关；假网关按键
  去重，返回 `deduped:true` 的既有扣款，本地只提交一次。
- **未转发键**：**绝不盲目重试**（可能重复扣款），也不提交本地副作用；
  键保持 `processing`，返回 503 要求人工/对账介入。这正是本地幂等层的边界。

### 崩溃恢复与租约

- 提交**之后**崩溃：WAL 里已有 completed 记录，重启后重试直接回放，无第二副作用。
- 提交**之前**崩溃：WAL 里只有 `processing` 占位（外部可能已扣款）。重启后在
  `-lease` 时间内仍返回「处理中」；租约过期后允许同键重新接管执行——配合
  转发的端到端键，网归去重，外部仍只一笔。短租约只是为了让崩溃演示更快，
  生产中应结合人工对账来决定接管时机。

---

## 5. 代码结构

```
cmd/idemresp/main.go        进程入口：同进程启动应用 + 假网关（两个端口）
internal/
  clock/                    时钟抽象：Real（墙上时间）/ Fake（可控时钟，测试用）
  digest/                   请求摘要（规范化 JSON + SHA256）
  txn/                       本地事务存储：WAL(fsync+CRC+断尾修复) + 状态机
    types.go                KeyRow / Order / LedgerEntry / 单条提交记录
    wal.go, frame.go        分帧、CRC32、fsync、崩溃断尾自动截断重放
    db.go                   Acquire / Fail / Commit、快照读、并发串行化
  gateway/                  本进程假支付网关（按键去重 + 故障注入 + metrics）
  client/                   故障注入 HTTP 客户端，三分类成功/失败/未知
  idem/                     传输无关的幂等业务服务（完整协议）
  server/                   HTTP 传输层与观测接口
e2e/                        真实 TCP 端到端测试，含子进程硬崩溃
examples/                   请求样例与样例响应
scripts/                    run-tests.sh（结构化结果）、manual-demo.sh
```

事务存储为何自己实现：本环境**无法访问 Go module 代理**，因此全程零第三方
依赖。WAL 采用 `[type][len][payload][crc32]` 分帧，每条记录 `fsync`；重放时
校验 CRC，遇崩溃造成的半截尾帧自动截断到上一条完整记录。

---

## 6. 自动化测试

```bash
go test -race ./...                 # 全部单元 + 端到端（含真实子进程崩溃）
./scripts/run-tests.sh             # vet + race + 生成 test-results.json 结构化结果
```

测试分层：

- `internal/txn`：占坑/提交/回放、冲突、处理中、失败可重试、**单事务原子性**、
  **WAL 重启重放**、**断尾修复**、32 并发 Acquire 仅一个获胜。
- `internal/digest`：规范化稳定性、载荷/方法区分、非法 JSON 拒绝。
- `internal/gateway`：键内去重、无键不去重、各类一次性故障（重置前/后、hang、500）。
- `internal/client`：成功 / 4xx / 5xx / 断连 / 超时的三分类。
- `internal/idem`：完整协议、并发 24 请求恰好一次、失败重试、有/无端到端键的
  未知结果策略、提交前后 context 取消、崩溃挂点。
- `internal/server`：HTTP 头、状态码、413、回放头、崩溃头开关。
- `e2e`：真实 HTTP 的并发重试、**提交前断线**、**提交后断线**、键内歧义恢复、
  无键歧义的边界演示、重启回放，以及用**重新执行测试二进制得到的真实子进程**
  验证 `os.Exit(77)` 的提交前/后硬崩溃恢复。

### 覆盖率（`go test -race -cover ./...`）

| 包 | 覆盖率 |
| --- | --- |
| internal/clock | 100.0% |
| internal/digest | 94.4% |
| internal/idem | 94.2% |
| internal/gateway | 91.9% |
| internal/server | 87.2% |
| internal/client | 87.7% |
| internal/txn | 83.7% |
| e2e | 76.4%（测试包本身） |
| cmd/idemresp | 0.0%（仅 main 启动胶水；行为由 e2e 真实启动覆盖） |

聚合语句覆盖率约 **80.7%**（`build/coverage.func.txt`）；所有**业务内部包
≥83.7%**。结构化结果：`test-results.json`，原始 `-json` 流：`build/test.log`。

---

## 7. 实际运行记录（本次交付）

以下命令均在交付时真实执行，结果如实记录：

- `go build ./...`、`go vet ./...`：通过。
- `go test -race -count=1 ./...`：全部通过，`pass events: 76, fail events: 0`
  （见 `build/run-tests.stdout`、`test-results.json`）。
- 真实启动服务（端口 18080/18081 被本机其他进程占用，改用 **29080/29081**）：
  - 首次 `201`；同键同体重试 `201 + Idempotent-Replay: true` 且响应体逐字节相同；
  - 同键异体重试 `409 idempotency_key_conflict`；
  - `X-Fault: reset-after-charge`（转发键）→ 自动去重，`deduped:true`，
    网关累计仍 1 笔、本地 ledger `count=1`；
  - `X-Forward-Key: false` 的未知结果 → `503 ambiguous_outcome`，本地 ledger
    `count=0`，而网关 metrics 显示外部已产生 1 笔扣款（边界演示）。
- 断线（客户端 `curl --max-time 0.3`，服务端延迟 1200ms）：
  - 提交**后**断线：重试即回放，ledger 仍 1；
  - 提交**前**断线：处理中重复得到 `409 request_in_progress`，分离的请求
    最终提交，随后重试回放，ledger 仍 1。
- 真实硬崩溃（`-allow-crash` + `X-Crash`，子进程退出码 **77**）：
  - 提交**前**崩溃：外部网关已扣 1 笔、本地无提交；重启并过 2s 租约后用同键
    重试，网归去重 `deduped:true`，本地 ledger 1、网关累计仍 1；
  - 提交**后**崩溃：由自动化测试 `TestE2E_CrashAfterCommit` 覆盖并通过
    （手动复跑该命令时被环境权限策略拦截，未强行绕过，故此处以自动化结果为准）。

未通过 / 偏差项（如实说明）：

- 交付中途的一次失败：脚本化假网关最初按「单次 Execute 的 attempt 序号」取脚本，
  导致「先失败后重试」用例失败，已改为按**全局调用次序**消费脚本并转绿。
- 一次 `go vet` 拦截：部分测试对 `http.Get` 的错误未先检查即使用响应，
  已全部改为先判错，`go vet` 现通过。
- 默认端口 18080/18081 在本机被无关进程（`cc-server` / `breaker-server`）占用，
  手动验收改用 29080/29081；程序行为不受影响。
- 环境无法访问 `proxy.golang.org`，因此**没有也无法使用第三方依赖**，
  事务引擎为标准库自实现；这是环境约束，不是功能裁剪。

---

## 8. 明确的保证与不保证

**保证（在本系统的本地事务边界内）：**

- 同一 `Idempotency-Key` + 同一请求摘要，业务处理与本地事务副作用**至多一次**，
  完成后的任意次数重试返回**同一份已保存响应**；
- 同键不同体 → 明确冲突；处理中重复 → 明确处理中响应；
- 结果、响应、本地副作用在**同一本地事务**原子提交，崩溃/断电不产生半提交；
- 并发重试、提交前/后断线、提交前/后进程崩溃后恢复，本地副作用计数恒为 1。

**不保证：**

- **不保证任意外部调用恰好一次。** 当外部调用结果未知且未使用贯穿下游的
  端到端幂等键时，外部系统可能已生效；本地数据库事务无法回滚或证实一个
  已经离开本进程的外部动作。要对外部效果做到近似恰好一次，必须把同一幂等键
  传到下游并由下游去重（本项目默认如此，并保留关闭开关以暴露边界），
  未知结果需对账兜底。

本项目不含任何前端，仅提供 HTTP API、样例与自动化测试。
