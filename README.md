# 并发令牌预算（Concurrent Token Budget）

一个**纯后端**的分层（全局 + 租户）令牌桶调度库与本地 HTTP 服务，Go 实现。

- **两层原子扣减**：一次请求在同一个临界区内同时支付“全局层”和“租户层”；任一层余额不足则整笔失败，**两层都不会有任何部分消费**。
- **单调时钟补充**：令牌只按单调时钟经过的时间补充；满桶不积累、不凭空产生存量。
- **动态速率**：可在运行时替换速率/突发；切换只保留存量，新速率从该存量向前补充，**绝不补满**，更小的突发只会向下钳制。
- **无浮点**：速率是精确有理数（分子/分母），存量是 1e6 定点整数（microtoken），中间乘法用 128 位整数；传输层拒绝分数 JSON 数字。
- **时钟与执行器可替换**：生产用真实单调时钟，测试用可精确推进的虚拟时钟；执行器（拿到预算后如何跑任务）是接口，可替换。
- **结构化事件**：每次准入决策与配置变更都产生结构化事件，可经内存接口查询或追加为 JSONL 文件。

无前端。

---

## 目录结构

```
go.mod
cmd/tokenbudgetd/main.go        # 本地 HTTP 服务入口
internal/
  clock/                        # 时钟抽象：Real（真实单调）/ Virtual（可推进、可批量唤醒）
  u128/                         # 128 位无符号整数乘除（对 math/big 差分测试）
  rational/                     # 精确有理速率、精确小数令牌解析（无 float）
  budget/                       # 核心：桶、两层限流器、调度器、可替换执行器、事件
  httpapi/                       # HTTP 接口
examples/
  config.json                   # 示例配置（含 1/3 精确速率与租户覆盖）
  requests.sh                   # 逐步请求样例
  concurrent.sh                 # 同时请求零超发样例
```

## 模型

- 全局层：所有租户共享一个桶。
- 租户层：每个租户一个桶；未在配置里显式覆盖的租户使用 `default` 配置。
- 一次 `cost=C` 的请求被允许，当且仅当 `global.available >= C` **且** `tenant.available >= C`；
  成功后两层各扣 `C`，失败则两层都不变。检查与扣减在单个互斥锁临界区内完成。
- 桶初始为满（允许等于突发容量的瞬时突发）。
- 补充公式（精确，无浮点）：

  ```
  microtokens_added = floor( (rate_num * elapsed_ns + carry) / (rate_den * 1000) )
  ```

  余数 `carry` 结转到下一次补充，因此 `1/3 token/秒` 这样的速率在任意时长上都不会漂移或凭空取整。
  补充后超过容量的部分丢弃（满桶不增值）。

## 精确性与溢出

- 令牌存量单位是 **microtoken**（1 token = 1,000,000），`int64` 定点数。
- 速率 `num/den` 为约分后的 `int64` 分数；`num * elapsed_ns` 用内部 128 位整数计算，
  支持的速率上限为 1e9 token/秒、突发上限 1e9 token、最慢速率 1 token / 1e12 秒。
- HTTP 上所有令牌数与速率都用**字符串精确小数/分数**表达；分数 JSON 数字（如 `0.1`）
  会被拒绝，因为它在 float64 下是 `0.1000000000000000055…`。

---

## 构建与运行

需要 Go 1.22+。

```bash
go build -o bin/tokenbudgetd ./cmd/tokenbudgetd

# 真实单调时钟（默认）
./bin/tokenbudgetd -addr 127.0.0.1:8080 -config examples/config.json

# 虚拟时钟（时间只在调用 /internal/clock/advance 时前进，便于确定性演示/测试）
./bin/tokenbudgetd -virtual -addr 127.0.0.1:8080 -config examples/config.json \
  -events-file events.jsonl
```

参数：

| 参数 | 说明 |
|---|---|
| `-addr` | 监听地址，默认 `127.0.0.1:8080` |
| `-config` | JSON 配置文件；省略则使用内置默认（global 10/s burst 10，tenant 2/s burst 5） |
| `-virtual` | 使用虚拟时钟，并开放 `POST /internal/clock/advance` |
| `-events-file` | 把结构化事件以 JSON Lines 追加到文件（同时仍可在 `/v1/events` 查询） |

配置文件格式见 `examples/config.json`：

```json
{
  "global":  { "rate": "10", "burst": "10" },
  "default": { "rate": "2",  "burst": "5" },
  "tenants": {
    "vip": { "rate": "1/3", "burst": "3" }
  }
}
```

`rate` 支持：`"10"`、`"10/s"`、`"0.5"`、`"1/3"`，或对象
`{"tokens":1,"per_seconds":3}`。`burst` 是精确小数字符串，最多 6 位小数。

---

## HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `GET  /healthz` | 健康检查，返回当前时钟时间 |
| `POST /v1/acquire` | 两层准入决策（非阻塞；给 `timeout_ms` 则阻塞等待） |
| `GET  /v1/buckets/{tenant}` | 查看全局与某租户补充后的桶快照（不消费） |
| `GET  /v1/config` | 查看当前配置 |
| `PUT  /v1/config` | 原子替换全部配置（动态速率/突发） |
| `GET  /v1/events?tenant=&limit=` | 查询结构化事件（可按租户过滤） |
| `POST /internal/clock/advance` | 仅 `-virtual`：推进虚拟时钟，体为 `{"ms":n}` 或 `{"ns":n}` |

### 准入请求

```bash
curl -s -X POST 127.0.0.1:8080/v1/acquire \
  -H 'Content-Type: application/json' \
  -d '{"tenant":"acme","cost":"1"}'
```

- 成功返回 `200` 且 `"allowed": true`；失败返回 `429` 且 `allowed=false`，
  `reason` 为 `denied_global` 或 `denied_tenant`，并带精确的 `retry_after`。
- `cost` 必须是字符串精确小数（`"1"`、`"0.25"`），分数 JSON 数字返回 `400`。
- 加 `"timeout_ms": 1000` 则阻塞等待（使用可调度时钟 sleep），在预算就绪后返回；
  超时返回 `429` 且 `timed_out=true`，**等待期间不预留、不部分消费**。

响应中的 `global` / `tenant_bucket` 是决策那一刻两层桶的精确快照。

更多可直接运行的样例见 `examples/requests.sh` 与 `examples/concurrent.sh`。

---

## 结构化事件

`acquire` 事件示例（JSONL 每行一个）：

```json
{"type":"acquire","at":"2026-01-01T00:00:00Z","request_id":"req-6",
 "tenant":"a","result":"denied_tenant","cost":"1",
 "global":{"available":"5","capacity":"10","rate":"10/s","last_refill":"..."},
 "tenant_bucket":{"available":"0","capacity":"5","rate":"2/s","last_refill":"..."},
 "retry_after":500000000}
```

`config` 事件在每次 `PUT /v1/config` 成功后产生，携带完整新配置视图。
执行器跑完任务后产生 `execute` 事件（含任务错误信息）。
事件在锁外投递，Sink 是接口，可替换为内存环（默认）、JSONL 文件（`MultiSink` 组合）或自定义后端。

## 作为库使用

```go
import "tokenbudget/internal/budget"

cfg := budget.Config{
    Global:  budget.BucketConfig{Rate: rational.PerSecond(10), BurstMicro: 10 * budget.MicroPerToken},
    Default: budget.BucketConfig{Rate: rational.PerSecond(2),  BurstMicro: 5 * budget.MicroPerToken},
}
lim, _ := budget.New(cfg, clk, sink)      // clk 可传虚拟时钟
d := lim.TryAcquire("acme", budget.MicroPerToken) // 非阻塞原子两层决策
// d.Allowed / d.Reason / d.Global / d.TenantSnap

sched := budget.NewScheduler(lim, myExecutor, sink) // myExecutor 实现 budget.Executor
r := sched.Submit(ctx, "acme", budget.MicroPerToken, func(ctx context.Context) error {
    // 只有在两层都拿到预算后才会执行
    return nil
})
```

---

## 测试

```bash
go test -race -count=1 ./...
```

覆盖（均为确定性测试，使用虚拟时钟，无真实 sleep）：

- **突发**：满桶恰好放行 `burst` 个，下一个被拒；按经过时间精确补充。
- **两层独立约束**：租户有余额但全局耗尽时拒绝全局。
- **失败无部分消费**：租户够、全局不够时，租户余额与全局余额都保持不变；超额 cost 不移动任何一层。
- **精确速率/无浮点**：`1/3 token/秒` 在 300 个 1 秒步长上恰好放行 100 次，且每个 3 秒窗口只产生 1 个令牌，无漂移、无提前取整；microtoken 的 `0.25` 恰好 4 次耗尽 1 token。
- **动态配置切换**：提速/加大突发不补满存量；缩小突发向下钳制；切换后按新速率补充。
- **同时请求**：200 个 goroutine 同一瞬间到达，恰好 `burst` 个成功、其余 429，两层精确归零；100 个不同租户时由全局层精确封顶。`-race` 下无数据竞争。
- **可替换执行器**：被拒准入绝不调用执行器；执行器错误如实进入回执与事件。
- **时钟**：虚拟时钟的精确到时唤醒、批量唤醒、取消清理；真实时钟的 context 取消。
- **HTTP**：基于 `httptest` + 虚拟时钟的端到端行为、状态码、并发零超发、浮点拒绝、配置切换。
- **128 位整数**：对 `math/big` 做 10,000 组随机 + 边界差分测试。

---

## 实际运行记录（本仓库开发时实测）

以下命令与结果在交付前实际执行（Go 1.22.2 / linux-amd64）：

- `go build ./...`、`go vet ./...`：通过。
- `go test -race -count=1 ./...`：5 个包全部 `ok`，无数据竞争。
- 用 `-virtual` 启动后用真实 `curl` 验证：
  - 租户 a 突发 5 个全 `200`，第 6 个 `429 denied_tenant`，快照显示租户 0、全局 5（拒绝未改动任何一层）。
  - 租户 b 耗尽全局后，全新租户 c 请求为 `429 denied_global`，其租户桶仍为满 `5`、全局仍 `0`（无部分消费）。
  - 推进 500ms：全局 `+5`、租户按 2/s `+1`，精确。
  - vip（1/3 token/秒）：1 秒后积 `0.333333` 仍 429；累计 3 秒恰好放行 1 个，紧随的第二个 429。
  - `PUT /v1/config` 提速到 100/s、突发 20：切换瞬间存量保持 `9/5` 未补满；推进 10ms 后各 `+1`。
  - 50 个真实并发请求（两层 burst=7）：结果恰为 `7 × 200` 与 `43 × 429`，请求后两层均精确为 `0`。
  - 分数 JSON 数字 `cost:0.1` 返回 `400`；字符串 `"0.25"` 连续 4 次成功（0.75→0.5→0.25→0），第 5 次 `429`。
  - 阻塞获取 `timeout_ms=80` 在虚拟时钟不推进时实际耗时 `0.0808s` 返回 `429 timed_out`。
  - `examples/requests.sh`、`examples/concurrent.sh` 实跑退出码 0。
- 未通过项：开发过程中修复的问题已全部在上述测试与实跑中复测通过，交付时 `go test -race` 无失败用例。
  （环境中预先存在的 `/tmp/tb_events.jsonl` 含其他程序写入的 4 条不同 schema 记录，与本项目无关；
  本项目使用独立的事件文件验证，结构见上。）
