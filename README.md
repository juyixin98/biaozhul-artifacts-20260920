# 重试预算传播（Retry Budget Propagation）

一个**纯后端**教学/验证项目：在 `client → edge → mid → leaf` 四层真实
loopback HTTP 调用中，实现**统一的重试预算与绝对截止时间传播**，只有被
**显式标记为可安全重试**的操作才会被重放；指数退避带**可注入随机源**；
所有"外部依赖"都是**本进程脚本化假服务**；时钟可注入，测试结果结构化。

不连接任何生产系统，无前端。

---

## 1. 它解决什么问题

朴素的"每层各自重试"会造成**重试风暴 / 放大**：若三层都允许重试 3 次，
一次根调用最坏可产生 `3 × 3 × 3 = 27` 次下游冲击。

本项目让**整条调用链共享同一个根预算**：

- 一个**全局尝试计数器**，跨层、跨进程、并发安全；
- 一个**绝对截止时间**（deadline），逐层以 wire header 传播；
- 每层可有**更紧的本地重试上限**，但永远无法超过根预算；
- 下游"本地重试上限用尽"时，上游仍可用剩余根槽位发起**一条全新链路**；
  而"根预算用尽/截止时间到"是**全局终止信号**，任何层都不得再试。

核心不变量（每个验收用例都断言）：

```
各层尝试数之和 == 全局根计数器 == 真实 HTTP 尝试数 ≤ 根预算上限
```

---

## 2. 目录结构

```
.
├── go.mod
├── cmd/server/             本地演示 HTTP 服务（/suite、/mesh/{preset}）
├── internal/
│   ├── clock/              时钟抽象：Real / Fake（手动推进）/ Auto（虚拟快进）
│   ├── retry/              核心：Budget、Do 重试循环、错误分类、指数退避
│   ├── propagation/        跨进程预算头 X-Retry-* 的注入/解析
│   ├── fault/              脚本化假依赖（按请求 ID 独立游标）
│   ├── report/             结构化事件时间线收集器
│   ├── mesh/               三层 HTTP 网格：leaf 假服务 + edge/mid 重试层
│   ├── harness/            声明式场景运行器（产出结构化 Outcome）
│   └── demo/               内置验收套件（7 个场景 + 断言）
├── e2e/                    三层故障夹具验收测试（Go test）
└── examples/               请求样例与真实响应快照
    ├── REQUESTS.md
    └── sample-output/
```

---

## 3. 核心概念

### 3.1 预算（`internal/retry/budget.go`）

- `NewRootBudget(clock, maxAttempts, deadline)` 建立根预算；
- `root.Child(localMax)` 派生子预算：**共享根计数器与截止时间**，另加本层
  本地上限；本地用尽返回 `ErrLocalExhausted`（上游可继续），根用尽返回
  `ErrBudgetExhausted`（全局终止）；
- `Reserve()` 预订一个尝试槽，**立即计数且不退回**（被取消/失败的尝试也
  真实消耗了资源）；
- `Restore(clock, max, used, deadline)` 在被调用方根据传播头**重建**预算
  视图，`AckUsed(n)` 以单调 max 方式对账下游已消耗量。

### 3.2 只有"显式安全"才可重试（`internal/retry/error.go`）

失败必须被显式分类，**默认失败安全（不重放）**：

| 构造器 | 含义 | 是否重试 |
|---|---|---|
| `retry.Retryable(op, err)` | 幂等/安全的瞬时错误（如 503/429） | 是 |
| `retry.RetryableAfter(op, err, d)` | 带服务端 Retry-After 提示 | 是 |
| `retry.NonRetryable(op, err)` | 确定性错误（如 400） | 否 |
| 未分类的普通 error | 未知，**不盲目重放** | 否 |
| `context.Canceled` / 截止时间到 | 取消/超时 | 否 |

`*Exhausted` / `*DeadlineExceeded` / `*LocalExhausted` 是预算层裁决。
本层操作者若把下游 `LocalExhausted` 显式 `Retryable(...)` 包一层，则它
只对**本层循环**生效（`Classify` 优先采用最外层 `ClassifiedError` 的本层
决定）。

### 3.3 指数退避 + 可注入随机源（`internal/retry/retry.go`）

```
delay = BaseDelay × Multiplier^(attempt-1)，上限 MaxDelay
delay *= 1 + Jitter × (2 × Rand() − 1)        // Rand 可注入，Jitter=0 完全确定
Retry-After 作为下限：最终等待 = max(指数退避, 服务端 Retry-After)
```

`Config.Rand func() float64` 即**可注入随机源**；测试注入确定性序列，
生产用 `rand.Float64`。等待超过剩余截止时间时会被裁剪，随后以
`deadline-exceeded` 终止。

### 3.4 跨进程传播（`internal/propagation`）

| Header | 含义 |
|---|---|
| `X-Request-Id` | 关联 ID（假服务按它维护独立故障游标） |
| `X-Retry-Deadline` | **绝对**截止时间（RFC3339Nano，UTC） |
| `X-Retry-Max` | 根预算总尝试上限 |
| `X-Retry-Used` | 上游已消耗槽位（响应头也回传，供调用方对账） |

另有 `X-Retry-Verdict`（响应）传播 `ok / retryable / non-retryable /
local-exhausted / budget-exhausted / deadline / canceled`。

---

## 4. 快速开始

要求 Go 1.22+，无第三方依赖（仅标准库）。

```bash
go test ./...                      # 全部测试
go test -race ./...                # 带竞态检测
go test ./... -coverprofile=c.out  # 覆盖率
go tool cover -func=c.out | tail -1

go run ./cmd/server -addr 127.0.0.1:8080
```

### HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针 |
| GET | `/suite` | 运行内置 7 个验收场景（虚拟时钟，瞬时），返回结构化 JSON |
| GET | `/mesh/{preset}` | 发起**一次真实**四层调用（真实 loopback + 真实退避等待） |

`preset`：`budget-cap`、`retry-after`、`deadline`、`non-retryable`、`success`。
`/mesh` 支持查询参数 `max=`（根预算）与 `deadline_ms=`（根截止时间）。

> 注意：8080/8099 等端口可能已被本机其他进程占用，可用 `-addr` 换端口。

---

## 5. 验收结果（实跑记录）

命令与完整原始输出见 [`examples/REQUESTS.md`](examples/REQUESTS.md)，
结构化 JSON 快照见 [`examples/sample-output/`](examples/sample-output/)。

`GET /suite`（虚拟时钟）实跑摘要：

| 场景 | 终态 | 已用/根预算 | client/edge/mid/leaf |
|---|---|---|---|
| root-budget-cap | budget-exhausted | **8/8** | 1/1/3/3 |
| local-cap-upstream-retry | budget-exhausted | **12/12** | 2/2/4/4 |
| server-retry-after-recovery | success | 6/10 | 1/1/2/2 |
| retry-after-vs-deadline | deadline-exceeded | 4/20 | 1/1/1/1 |
| client-cancellation | canceled | 6/100 | 1/1/2/2 |
| non-retryable-400 | non-retryable | 4/10 | 1/1/1/1 |
| immediate-success | success | 4/10 | 1/1/1/1 |

三项明确的验收点：

1. **三层故障夹具，总尝试数受根预算限制**：叶子持续 503、三层都愿意重试，
   根上限分别取 4/8/12 时，真实总尝试数**精确等于**根上限（见
   `e2e/acceptance_test.go::TestRootBudgetCapsThreeTierAmplification`）。
2. **服务端 Retry-After**：叶子首次 `429 + Retry-After: 1`，真实等待
   `1000ms` 后第二次成功（`live-retry-after.json`）；超过截止时间的
   Retry-After 则以 `deadline-exceeded` 终止。
3. **取消**：真实时钟下 40ms 后取消，调用以 `canceled` 展开且记账守恒；
   **预算耗尽**：`budget-exhausted`，且计数守恒。

### 测试与覆盖率（本地实跑）

```
go vet ./...        -> 通过
gofmt -l .          -> 无输出（干净）
go test ./... -race -count=1 -> 全部包 ok，58 个测试用例，0 失败
总语句覆盖率        -> 87.1%（≥ 80% 要求）
```

未通过项：**无**。

---

## 6. 设计取舍与边界

- **绝对截止时间而非相对超时**跨网络传播，避免逐跳叠加；接收方用注入时钟
  在 `Reserve/Precheck` 处强制，因此虚拟时钟下的截止时间测试是确定的。
- **叶子在门口拒绝**：当上游已耗尽根槽位，叶子收到请求时不再执行假服务，
  直接回 `budget-exhausted`。这是一次"接触"但**不是一次服务尝试**，在
  结构化结果中单独记为 `leaf_rejected`，不计入尝试数之和。
- **取消时的记账**：被取消时仍在飞行中的下游预订可能无法经响应头回传，
  故 harness 以共享事件时间线上的**全局尝试号**为准对账。
- 范围限定：单进程、loopback、脚本化故障；不做真实网络弹性、持久化或前端。
