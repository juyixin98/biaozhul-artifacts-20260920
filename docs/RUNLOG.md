# 运行与验证记录（RUNLOG）

本文件如实记录在交付环境中实际执行的命令与结果。日期：2026-09-23。
环境：Linux 6.8（amd64），Go 1.22.2，无第三方依赖（仅标准库）。

## 1. 构建

```
$ go version
go version go1.22.2 linux/amd64

$ gofmt -l .
（无输出 = 全部符合 gofmt）

$ go vet ./...
vet OK

$ go build ./...
build OK

$ go build -o bin/tokenbudget ./cmd/tokenbudget
（生成 bin/tokenbudget）
```

## 2. 自动化测试

```
$ go test -count=1 ./...
?   tokenbudget/cmd/tokenbudget    [no test files]
?   tokenbudget/internal/event     [no test files]
ok  tokenbudget/internal/budget    0.068s
ok  tokenbudget/internal/clock     0.002s
ok  tokenbudget/internal/httpapi   0.005s

$ go test -race -count=1 ./...
ok  tokenbudget/internal/budget    1.520s
ok  tokenbudget/internal/clock     1.015s
ok  tokenbudget/internal/httpapi   1.022s
```

- 用例总数：**50 个全部 PASS，0 个 FAIL/SKIP**（`go test -v ./... | grep -c -- '--- PASS'` = 50）。
- 竞态检测（`-race`）下全部通过。
- 覆盖的验收点：
  - **突发**：`TestBucketBurstExact`、`TestBucketReservationBurstThenPace`；
  - **虚拟时间精确验证**：`TestBucketRefillExactPerSecond`、`TestBucketWaitExact`、
    `TestSchedulerVirtualTimeExecution`、`TestSchedulerTwoLayerPacing`、时钟包 6 个用例；
  - **浮点避免**：`TestBucketFractionalRateNoFloatDrift`、`TestBucketTenthTokenPerSecondExact`
    （1 token/10s 用 100 个 100ms 小步精确得到 1 个令牌），
    128 位除法对照 `math/big` 的属性测试 `TestDiv128PropertyBigInt`（20000 组随机输入）、
    `TestDivCeilProperty`（20000 组）；
  - **配置切换不凭空增发**：`TestConfigRateIncreaseNoRetroactiveTokens`、
    `TestConfigRateDecreasePreservesWhole`、`TestConfigCapacityShrinkGrow`、
    `TestConfigStopAndResume`、`TestTwoLayerConfigChangeEvent`；
  - **同时（并发）请求**：`TestTwoLayerConcurrent`、`TestTwoLayerConcurrentManyTenants`、
    `TestSchedulerConcurrentReservationsAtomic`、`TestTwoLayerStockConservation`
    （50 轮 × 16 协程随机并发，断言两层存量守恒）；
  - **扣减失败无部分消费**：`TestTwoLayerGlobalBlocksNoPartial`、
    `TestTwoLayerTenantBlocksNoPartial`、`TestTwoLayerExceedCapNoConsumption`、
    `TestSchedulerRejectOversize`、`TestSchedulerHorizon`；HTTP 层
    `TestRequestAllowedThenDeniedNoPartial`、`TestScheduleRejectedOversize`。

## 3. 真实 HTTP 端到端运行

```
$ ./bin/tokenbudget -addr 127.0.0.1:18100 \
    -config examples/config.json -events /tmp/tb_ship_events.jsonl
2026/... tokenbudget listening on 127.0.0.1:18100 (horizon=24h0m0s)

$ bash scripts/e2e_demo.sh http://127.0.0.1:18100 > docs/e2e_transcript.txt 2>&1
demo exit=0
```

状态码分布（完整请求/响应正文见 `docs/e2e_transcript.txt`）：

| HTTP | 次数 | 含义 |
|------|------|------|
| 200  | 14 | health/state/成功扣减/配置成功 |
| 202  | 2  | 两个调度作业被接收，分别在约 1s、2s 后执行成功 |
| 400  | 3  | 缺租户、JSON 非法、capacity=0 |
| 422  | 2  | 请求/调度超过桶容量（零消费） |
| 429  | 3  | 全局层或租户层不足（零消费） |

关键观测（均来自 `docs/e2e_transcript.txt`）：

1. 三层扣减序列：全局突发被调到 3，acme/beta 各消费到全局清空后，
   再发 `beta x1` 得到 **429**，响应体里 `beta.available` 仍为 **2**
   （租户层没有被部分消费），`global.available=0`。
2. 扩容量 3→50 后 `global.available` 仍为 0（**不凭空增发**）；
   beta 缩容 3→1 把存量从 2 钳到 1。
3. acme 从停止切到 1 token/s 后立刻请求仍 **429**（切换不立即造令牌）；
   两个调度的 `ready_at_ns` 精确相隔 1,000,000,000 ns，3 秒后状态均为
   `succeeded`。
4. `-events` 文件每行一个合法 JSON 对象，`seq` 单调、含全部状态变更类型。

## 4. 开发过程中发现并修复的问题（如实记录）

- **未来预留的记账缺陷（设计层）**：初版 `Reserve` 用“现在投影 + 乐观重试”，
  在多个未来预留并存时会因锚点被推进到未来而出现无效重试甚至超额承诺。
  改为 Guava 风格“下一可用令牌时刻”模型（允许锚点落在未来），在**单次
  持锁**内取两层就绪时刻的较大值并同时提交，消除读-判-提交窗口。
  由 `TestBucketReservationQueue`、`TestSchedulerTwoLayerPacing`、
  `TestSchedulerConcurrentReservationsAtomic` 守护。
- **停止→恢复补涨**：`applyConfig` 最初未处理 rate 0→正 的情况，恢复后会
  把停止期间也补算。已在恢复时重置锚点到当前时刻（`TestConfigStopAndResume`）。
- **错误被 `%v` 包装导致 `errors.Is` 失效**：`Scheduler.Submit` 用
  `fmt.Errorf("%w: %v", ErrRejected, err)` 丢失了内层错误，HTTP 层把
  “超容量”误判成 429。改为 Go 1.20+ 的双 `%w`（`TestScheduleRejectedOversize` 守护）。
- **拒绝时 `wait_ns` 出现负值**：停止补充且存量为空的一层被错误地按
  “就绪时刻=现在”参与求最大值。已在该层返回错误时不贡献有限等待，
  最终 `wait_ns=0`（`TestTwoLayerStoppedLayerWaitHintNotNegative` 守护）。
- 常量 `MaxRateDen` 最初误写成“秒/年”而非“纳秒/年”，早期桶测试即暴露并修正。

## 5. 未通过项 / 已知限制

- 交付时 `go test ./...`、`go test -race ./...` 全部通过，**无未通过用例**。
- 已知设计取舍（非缺陷）：仅整数令牌；预留为坚定语义、取消不退款；
  状态仅内存（事件可 JSONL 落盘审计）；单机实现，不做多实例共享存储。
  详见 `docs/design.md` 第 8 节。
