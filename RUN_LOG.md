# 运行记录（RUN_LOG）

本文件如实记录项目开发与验收过程中**实际执行的命令、结果，以及出现过的失败项
与修复**。环境：Linux 6.8、Go 1.22.2、x86_64。所有命令均在项目根目录执行。

## 1. 最终验收结果

### 1.1 自动化测试（含竞态检测）

命令：

```bash
go test -race -timeout 120s ./...
```

结果（实际输出）：

```text
?   	breakerhalfopen/cmd/runscenarios	[no test files]
?   	breakerhalfopen/cmd/server	[no test files]
ok  	breakerhalfopen/internal/appclient	1.029s
ok  	breakerhalfopen/internal/breaker	1.022s
ok  	breakerhalfopen/internal/clock	(cached)
ok  	breakerhalfopen/internal/httpapi	1.079s
ok  	breakerhalfopen/internal/scenario	1.039s
ok  	breakerhalfopen/internal/upstream	(cached)
```

无竞态报告，全部通过。

覆盖率（`go test -cover ./internal/...`，实际输出）：

```text
ok  	breakerhalfopen/internal/appclient	coverage: 94.9%
ok  	breakerhalfopen/internal/breaker	coverage: 93.3%
ok  	breakerhalfopen/internal/clock	coverage: 73.5%
ok  	breakerhalfopen/internal/httpapi	coverage: 69.6%
ok  	breakerhalfopen/internal/scenario	coverage: 95.4%
ok  	breakerhalfopen/internal/upstream	coverage: 92.6%
```

说明：核心包（breaker/scenario/upstream/appclient）均在 92% 以上；clock 与
httpapi 较低，未覆盖部分主要是 Real 时钟薄封装、index 文本与日志中间件。

### 1.2 验收场景（结构化报告）

命令：

```bash
go run ./cmd/runscenarios -out reports
```

实际输出：

```text
[PASS] late_failure             steps=30 pass=true
[PASS] probe_contention         steps=31 pass=true
[PASS] cancel_is_not_failure    steps=44 pass=true

3/3 scenarios passed; reports in reports/
```

逐步机器可读报告：`reports/late_failure.json`、`reports/probe_contention.json`、
`reports/cancel_is_not_failure.json`、`reports/summary.json`。每个步骤记录
虚拟时间、动作（call/await/advance/expect）、当次尝试、断路器快照与断言是否通过。

### 1.3 本地 HTTP 端到端

命令：

```bash
go build -o /tmp/breaker-server ./cmd/server
/tmp/breaker-server -addr 127.0.0.1:18081
./examples/demo_late_failure.sh http://127.0.0.1:18081
```

关键实际结果：

- 3 次注入失败后 `/api/state` 为 `open`，代际 1；
- OPEN 期间调用返回 **HTTP 503**，body 中 `result:"rejected"`，且假下游
  active 调用数不变（被拒请求未触达下游）；
- `{"duration_ms":10000}` 推进虚拟时钟后状态变 `half_open`，代际 2；
- 放行挂起的旧调用，其结果 `result:"failure" generation:0`（迟到的旧代结果），
  随后 `/api/state` 仍为 `half_open`、代际 2、`probe_successes:0`——新代状态
  未被改变（仅 `total_failures` 从 3 增到 4 这一观测计数）；
- 两个探测成功后状态 `closed`、代际 3；
- 三个内置场景经 HTTP 调用均 `success=true pass=true`（30/31/44 步）。

完整响应样例可按上面的脚本现场复现。

## 2. 开发过程中实际出现的失败与修复（如实记录）

下列问题都在开发中真实出现并被修复，记录以便理解设计中几处关键约束的由来。

### 2.1 编译错误（首轮 `go build ./...`）

- `scenario/runner.go`：`Snapshot` 辅助函数返回指针后又取址（`**Snapshot`）；
  快照中 `int` 字段直接赋给 `uint64` 计数器。
- 修复：理清指针/值；对窗口与在飞计数做显式 `uint64(...)` 转换。

### 2.2 【关键】虚拟时钟下测试整包挂死（定时器注册竞态）

现象：首次 `go test -race ./...` 时 `upstream` 与 `scenario` 两个测试二进制
长时间不退出（观察到运行 4 分钟以上），只能手动终止。

根因：`clock.WithTimeout` 原先在**新 goroutine 内部**才向虚拟时钟注册定时器；
测试主流程在 `WaitActiveN` 同步点后立刻 `Advance()`，存在"时钟已越过截止
时间，但定时器还没注册"的窗口，导致该定时器永远不会触发，调用永久挂起。

修复（两处，均为"先注册定时器，再对并发世界可见"）：

1. `internal/clock/context.go`：`clk.After(d)` 在返回前**同步**注册，再启动
   监听 goroutine；
2. `internal/upstream/upstream.go`：延迟指令的定时器在持有锁期间、`active++`
   与 `Broadcast()` **之前**注册。

修复后所有等待点（WaitActiveN）都能保证定时器已存在，场景完全确定性。

### 2.3 断路器单测对代际的期望时序错误

现象：`TestFailingProbeReopens`、`TestStalePermitCannotChangeNewGeneration`
报 `generation=3, want 2`。

根因：OPEN→HALF_OPEN 是**惰性转换**（无后台定时器，在
`Allow/State/Snapshot` 被观察时才发生）。测试推进时钟后直接读 `Generation()`，
此时尚未触发观察转换。

处置：这是测试用法问题而非产品缺陷（README 已明确该约定）。在读取代际前先
调用 `b.State()` 触发观察，断言随之正确。

### 2.4 计数断言把"零有意义"的字段按非零忽略处理

现象：场景报 `probe_permits_free want=1 got=2`、`in_flight want=0 got=2`。

根因有两层：(a) 初版 `ExpectCounters` 对 `in_flight`/`probe_permits_free`
采用"非零才比较"，反过来又对 got 非零强制比较，使只关心其他计数的断言被
牵连；(b) 半开探测名额语义是**并发槽**——探测一完成名额即归还（free 立刻
回到 2），与"探测成功占用名额直到关闭"的另一种语义不同。

处置：(a) 抽出语义明确的 `ExpectInFlight` / `ExpectProbePermitsFree`
显式断言（零也是要断言的值），`Counters` 只保留累计量；(b) 确认采用并发槽
语义（与 `MaxProbeCalls` = 最大并发探测 的定义一致），并按此给定期望值，
同时在场景中保留"替补探测"步骤验证槽位释放后可被重新获取。

### 2.5 假上游单测使用 1ms 延迟却无人推进虚拟时钟

现象：`TestScriptedSuccessFailureAndFallback` 挂在 `upstream.go` 的 select。

根因：第三条脚本指令写成 `{Delay: time.Millisecond}`，在虚拟时钟下必须有人
`Advance` 才会返回；该用例没有推进时钟。

修复：该用例本意是验证脚本与 fallback，改为立即成功指令 `{}`；延迟与时钟
取消的行为由专门用例（`TestDelayedCallCanceledByClockDrivenDeadline`）覆盖。

### 2.6 被断路器拒绝的调用返回了 HTTP 200

现象：HTTP 集成测试 `call during OPEN unexpectedly succeeded`。

根因：`/api/call` 对任何完成的尝试都返回 200，仅凭 body 区分，语义不清。

修复：`result=rejected` 时返回 **503**，包络 `success:false` 且
`error:"circuit breaker is open"`，同时仍在 `data` 中给出结构化尝试；
测试改为断言 503。

### 2.7 恢复 CLOSED 后探测计数未清零

现象：curl 端到端最终快照中 CLOSED 状态下 `probe_successes` 仍为 2。

根因：`enterClosedLocked` 清了滑动窗口但漏清 `probeSuccessesRun` 等半开字段。

修复：进入 CLOSED 时一并清零探测在飞、连续成功与失败标记。回归测试全绿。

## 3. 复现入口速查

```bash
go build ./...                                   # 编译
gofmt -l .                                       # 格式检查（无输出即干净）
go vet ./...                                     # 静态检查
go test -race ./...                              # 全量测试 + 竞态
go test -cover ./internal/...                    # 覆盖率
go run ./cmd/runscenarios -out reports           # 验收场景 + JSON 报告
go run ./cmd/server -addr 127.0.0.1:18080        # 本地演示服务
./examples/demo_late_failure.sh                  # curl 端到端演示
```

## 4. 未通过项 / 遗留说明

- 截至最后一次运行，**无未通过的测试或验收场景**。
- 未做前端（符合要求）。
- 未执行 `gosec`/`staticcheck`：本机环境未安装这些工具；已用 `go vet` 覆盖
  基础静态检查。服务仅监听回环地址、无密钥、无外部网络面。
