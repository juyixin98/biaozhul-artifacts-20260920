# 幂等响应保存（Idempotent Response Save）

一个**纯后端、纯本地**的 Go 项目：为「有副作用的 HTTP 请求」实现幂等层。
幂等键绑定请求摘要；执行结果与业务副作用在**同一个本地事务**中提交；
对并发重试、处理中重复请求、提交前/后断线、进程崩溃重启等情形给出明确语义，
并用故障注入客户端自动验收。**不接任何生产系统**——所有"外部依赖"都是跑在
本进程（独立子进程）里的假服务，通过真实 loopback HTTP 访问。

> 明确边界（本项目的核心结论）：幂等层只保证**本地事务副作用恰好一次**；
> **不保证任意外部系统调用恰好一次**。外部 HTTP 调用无法纳入本地事务，
> 在"外部已处理但响应回程丢失"时，调用方无法分辨，重试即可能产生外部重复。
> 场景 `external_not_exactly_once` 专门把这一点跑给你看。

---

## 1. 它解决什么问题

对一个会产生副作用的接口（示例为 `POST /v1/deposits` 存款：写一条流水 + 更新余额），
调用方在网络抖动/超时时会重试。没有幂等层时：

- 重试可能把 100 元存成 200 元（副作用执行多次）；
- 同键串用不同请求可能互相覆盖；
- 处理中的重复请求要么盲目再执行，要么得到含义不清的错误；
- 提交成功但响应丢失时，调用方不知道到底成没成。

本项目用「**先占位、后同事务提交**」的协议解决这些问题。

## 2. 协议与语义

### 2.1 幂等键绑定请求摘要

- 调用方在 `Idempotency-Key` 头提供键（一个逻辑请求一个唯一键）。
- 服务端对 `方法 + 路径 + 规范化正文` 计算 SHA-256 摘要 `fingerprint`（JSON 按键名排序、
  去空白，故字段顺序不同但语义相同的 JSON 视为同一请求）。
- 键与摘要一起存储：
  - **同键同摘要** → 同一请求，允许重放/等待；
  - **同键不同摘要** → `409 idempotency_conflict`，绝不执行第二个请求。

### 2.2 占位 → 执行 → 同事务提交

每个键的生命周期：`pending`（处理中占位）→ `completed`（已提交）。

1. **Begin（抢占占位）**：同键的并发请求中，只有一个能把 `pending` 记录 fsync 落盘；
   其余拿到明确结果（已完成→重放；处理中→等待/202；不同摘要→409）。
2. **执行外部步骤**：调用（假）外部审计系统。这一步**不在本地事务内**。
3. **Commit（同一本地事务提交）**：把【HTTP 执行结果】和【业务副作用（流水+余额）】
   写进**同一条 WAL 记录并 fsync**。原子边界就是这条记录——恢复时二者要么都在、
   要么都不在，不存在"副作用已入账但结果丢失"或反之。

读取路径只走内存快照；提交必须先落 WAL（含 fsync）再改内存，所以"观察到成功 ⇒ 已持久化"。

### 2.3 处理中重复请求：明确响应，不盲重

首个请求执行期间，同键同摘要的重复请求：

- 在有限时间（`-wait-max`，默认 10s）内**长轮询**首个请求的结局；
- 期间首个请求提交 → 直接返回 `200` + `Idempotency-Status: replayed`（同一份结果）；
- 超时仍未完成 → 返回 `202 Accepted` + `Retry-After: 1`，明确告知"首个请求仍在处理，
  请用**相同的键和正文**重试"，或 `GET /v1/idempotency/<key>` 查询。
  重复请求本身**永远不会**去执行业务。

### 2.4 断线、崩溃与 TTL 回收（代次 / generation 的 CAS）

占位带一个 TTL（`-pending-ttl`）。如果执行者在提交前断线/崩溃，`pending` 已 fsync 会保留：

- **TTL 内**：任何同键同摘要请求得到 `202`，不会重复执行；
- **TTL 后**：新请求可以**同摘要回收**占位，但以递增的**代次（generation）**接管；
  旧执行者即使"僵尸复活"迟到提交，也会因代次不匹配被拒绝（`lease_lost`），
  本地副作用绝不可能重复入账。

持久化的故障与恢复：

- **提交前断线**：占位遗留到 TTL；TTL 后重试成功。本地副作用 **1 次**；
  外部调用可能 **2 次**（断线前那次 + 重试那次）——见场景 5。
- **提交后断线**：事务已落盘；重试只重放，本地副作用与外部调用都 **1 次**——见场景 6。
- **提交前进程被 SIGKILL**：重启后从 WAL 恢复，占位仍在；TTL 后重试成功——见场景 7。
- **成功提交后进程被 SIGKILL**：重启后结果与副作用都从 WAL 恢复，重放返回同一结果——场景 8。

### 2.5 不保证什么（重要）

- **不保证任意外部系统调用恰好一次。** 外部审计通过真实 HTTP 访问，独立进程，
  不在本地 WAL 事务里。当外部"已落账但响应丢失"时，重试会让外部出现两条事件，
  而本地账本仍只有一笔（场景 9，报告里 `external events=2` 对比 `local effects=1`）。
- 要把外部调用也做成恰好一次，需要外部系统自身支持去重键（以 `Idempotency-Key` 做
  服务端去重）或分布式事务/对账，超出本项目范围。

## 3. 目录结构

```
.
├── cmd/
│   ├── server/       业务幂等 HTTP 服务（POST /v1/deposits 等）
│   ├── auditserver/  假外部审计系统（独立进程，真实 HTTP）
│   └── client/       idemcheck：故障注入验收客户端，输出结构化 JSON 报告
├── internal/
│   ├── idem/         请求摘要（键绑定正文，JSON 规范化）
│   ├── clock/        可控时钟（Fake，可持久化、重启不倒流）
│   ├── store/        WAL 存储：pending/commit 协议、代次 CAS、崩溃恢复
│   ├── fakeaudit/    假外部审计系统（成功/失败/先落账再失败/慢响应注入）
│   ├── auditclient/  外部审计 HTTP 客户端（带超时）
│   ├── service/      业务编排：占位→外部调用→同事务提交、处理中等待
│   └── api/          HTTP 处理器、故障注入头、观测/控制面
├── examples/         请求样例（requests.http 与 curl-examples.sh）
├── run/              最近一次 idemcheck 运行产物（report.json、日志、WAL）
└── README.md
```

## 4. HTTP 接口

业务服务（默认 `http://127.0.0.1:18080`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/deposits` | 幂等存款。需 `Idempotency-Key` 头；JSON 正文 `{"account","amount","memo?"}` |
| GET  | `/v1/idempotency/<key>` | 查看键当前记录（pending/completed、摘要、代次、结果、副作用） |
| GET  | `/admin/ledger` | 本地副作用：流水 + 余额 |
| DELETE | `/admin/ledger` | 清空存储（测试用） |
| GET  | `/admin/stats` | 存储计数 + 外部系统调用/事件计数 |
| POST | `/admin/reset` | 同时复位业务存储与（尽力）假外部系统 |
| POST | `/admin/clock/advance` | 假时钟模式下拨钟 `{"ms":31000}` |
| GET  | `/healthz` | 健康检查 |

响应头：

- `Idempotency-Status: created | replayed` —— 本次是首次执行还是重放；
- `Idempotency-Generation: <n>` —— 占位代次（被 TTL 回收重试过会递增）。

故障注入请求头（**仅本地验收用**，任何真实部署都不应信任）：

| 头 | 效果 |
|---|---|
| `X-Fault-Delay-Before-Commit-Ms: 400` | 外部调用后、提交前停留 N 毫秒（制造"处理中"窗口） |
| `X-Fault-Before-Commit-Disconnect: 1` | 提交前直接劫持并关闭 TCP 连接（不返回任何 HTTP） |
| `X-Fault-After-Commit-Disconnect: 1` | 提交并 fsync 后关闭连接（响应回程丢失） |
| `X-Fault-Crash-Before-Commit: 1` | 提交前 `os.Exit(2)`（模拟进程崩溃，pending 已落盘） |

假审计系统（默认 `http://127.0.0.1:18081`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/audit/events` | 写入审计事件 |
| POST | `/audit/faults` | 故障注入：`{"fail_next_n":n}`、`{"record_then_fail_next_n":n}`、`{"hang_ms":n}` |
| GET  | `/audit/inspect` | 查看调用次数与已接收事件 |
| POST | `/audit/reset` | 清空事件与故障状态 |

## 5. 快速开始

需要 Go 1.23+（仅用标准库，无第三方依赖）。

```bash
# 编译三个二进制到 ./bin
make build

# 终端 A：假外部审计系统
./bin/auditserver -addr 127.0.0.1:18081

# 终端 B：业务服务（假时钟，便于演示 TTL）
./bin/auditserver ...               # 已在 A 启动
./bin/idempotency-server \
  -addr 127.0.0.1:18080 \
  -audit-base-url http://127.0.0.1:18081 \
  -fake-clock -pending-ttl 30s -wait-max 10s

# 终端 C：跑一遍人工请求样例
bash examples/curl-examples.sh
```

### 一键自动化验收（推荐）

```bash
make accept
# 等价于：
# go run ./cmd/client -work ./run/acceptance -out ./run/report.json -keep
```

`idemcheck` 会自动：编译并以**子进程**拉起业务服务（假时钟）与独立的假审计系统
（随机端口、独立数据目录），跑完 9 个场景，打印结构化 JSON 报告，并在全部断言通过时
以退出码 0 结束。它还会真实地 `SIGKILL` 业务进程并重启，以验证崩溃恢复。

## 6. 测试

```bash
make test       # go test -race -cover ./...
make cover      # 生成覆盖率 profile 并输出函数级报告
make vet        # go vet ./...
make fmt        # gofmt -w .
```

- 单元/集成测试使用标准 `go test`（`httptest` 端到端，含真并发与 `-race`）。
- 内部包聚合语句覆盖率 ≥ 80%（实测约 82%；store 82%、api 86%、service 94%、
  idem 90%、clock 83%）。`cmd/` 主程序为装配代码，未计入该门槛。
- 端到端验收由 `cmd/client` 的 9 场景 / 55 断言承担。

最近一次真实运行的命令、结果与过程中发现并修复的问题，见 [docs/RUNLOG.md](docs/RUNLOG.md)。

## 7. 验收场景（idemcheck）

| 场景 | 验证内容 |
|---|---|
| `replay_basic` | 同键同正文第二次重放，同 tx_id，本地/外部各一次 |
| `conflict_different_body` | 同键不同正文稳定 409，无额外副作用 |
| `in_progress_concurrent` | 处理中的重复请求等待后拿到重放结果，而非盲重试/5xx |
| `concurrent_retries_once` | 24 个并发同键请求：1 个 created、23 个 replayed，同一 tx_id，账本一笔 |
| `disconnect_before_commit` | 提交前断线 → TTL 前 202、TTL 后成功；本地 1 次、外部可 2 次 |
| `disconnect_after_commit` | 提交后断线 → 重试只重放；本地与外部都 1 次 |
| `crash_recovery` | 提交前 SIGKILL → 重启后占位仍在；TTL 后成功；账本一笔 |
| `replay_survives_restart` | 成功提交后 SIGKILL → WAL 恢复结果与副作用，重启后重放同一结果 |
| `external_not_exactly_once` | 外部先落账再失败 → 重试后外部 2 条事件、本地 1 笔（**不保证外部恰好一次**） |

报告字段：每个场景含 `passed`、逐条 `assertions`、`requests`（含传输层错误原文）、
`evidence`（边界说明）、失败时的服务器日志尾部；顶层 `summary` 汇总计数。

## 8. 设计取舍 / 非目标

- **持久化用 fsync 过的 WAL + 内存快照**，纯标准库、无 CGO/外部数据库。它足以表达
  "本地事务原子提交与崩溃恢复"这一验收目标；不是生产级数据库（无 checkpoint/压缩，
  WAL 会随写入增长；`admin/reset` 会截断）。
- **TTL + 代次回收**是处理"执行者死亡遗留占位"的确定性机制。代价是极端情况下
  （超过 TTL 且旧执行者其实还活着）旧提交会被 `lease_lost` 拒绝——这是为
  "绝不重复副作用"付出的可用性代价，符合本项目把正确性放在首位的目标。
- **外部调用不在事务内**，因此本项目**刻意不承诺**跨系统恰好一次；
  它诚实地把"本地恰好一次、外部可能多次"的边界用证据展示出来。
- 无前端、无鉴权、无 TLS；故障注入头与管理端点只适合本地可信环境。
