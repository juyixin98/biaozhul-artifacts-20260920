# RESULTS.md — 实际测试与运行结果

本文件如实记录在交付环境中**实际执行**的命令与结果。环境：

- OS：Linux 6.8.0-90-generic (x86_64)
- Go：**go1.23.4 linux/amd64**（位于 `/usr/local/go`，`CGO_ENABLED=1`）
- 日期：2026-09-24
- 第三方依赖：**无**（仅标准库）

---

## 1. 自动化测试

命令：

```bash
go vet ./...
gofmt -l .          # 无输出 = 全部已格式化
go mod tidy         # go.mod 无变化；不生成 go.sum（零第三方依赖）
go test -race -count=1 -v ./...
```

结果：**go vet 干净；gofmt 无差异；27 个测试全部 PASS；`-race` 竞态检测器无任何告警。**

`hlc` 包（19 个）：

```
--- PASS: TestTickAdvancesWithinSameMillisecond
--- PASS: TestTickResetsLogicalWhenPhysicalAdvances
--- PASS: TestBurstSameMillisecondStrictIncreasing          # 同毫秒 1 万次突发严格递增
--- PASS: TestPhysicalClockRollbackKeepsMonotonicity        # 物理回拨保持单调
--- PASS: TestReceiveMergesRemote                           # 接收合并的四种分支
--- PASS: TestReceiveDuringRollback                         # 回拨期间接收远端
--- PASS: TestCausalChainThreeNodes                         # 三节点因果链严格递增
--- PASS: TestFutureDriftRejected                           # 漂移边界（=1000 接受，1001 拒绝）
--- PASS: TestHugeFutureValueRejected                       # 巨大未来值 2^62-1 被拒且不污染时钟
--- PASS: TestTickOverflowWaitsForPhysicalAdvance           # 溢出后等物理前进
--- PASS: TestTickOverflowWhenClockWillNotAdvance           # 时钟不前进 -> ErrOverflow
--- PASS: TestReceiveOverflowWaitsThenMerges
--- PASS: TestReceiveOverflowAtUint64LimitNoWraparound      # uint64 +1 回绕防护回归
--- PASS: TestReceiveOverflowBoundedWaitDoesNotHang         # 遥远目标快速失败、不挂起
--- PASS: TestReceiveRejectsLogicalAboveLimit
--- PASS: TestInvalidTimestamps
--- PASS: TestConcurrentTicksAreUniqueAndOrdered            # 64 goroutine × 200，无重复
--- PASS: TestSerializationRoundTripNoPrecisionLoss         # int64/uint64 全量程往返
--- PASS: TestCompareOrdering / TestNowIsTickAndEqual /
          TestDefaultPhysicalIsUnixMillis / TestDefaultWaitReturnsWhenTargetReaches /
          TestNewValidationAndDefaults
ok  hlc-service/hlc   (race enabled)
```

`server` 包（10 个，`httptest` 黑盒 HTTP）：

```
--- PASS: TestHTTPTickStrictlyIncreasing                    # HTTP 500 次 tick 严格递增
--- PASS: TestHTTPCausalChainThreeServers                   # 三个 HTTP 节点的因果链
--- PASS: TestHTTPReceiveAcceptsAllBodyShapes               # 4 种请求体形态
--- PASS: TestHTTPFutureDriftRejected422
--- PASS: TestHTTPBadBodies                                 # 空/坏 JSON/缺字段/负值
--- PASS: TestHTTPOverflowReturns503
--- PASS: TestHTTPStatusAndHealth
--- PASS: TestHTTPJSONPrecisionRoundTrip                    # 大整数 HTTP JSON 无损
--- PASS: TestHTTPJSONUint64MaxOverflowProvesWraparoundFixed
--- PASS: TestHTTPNowPeekDoesNotAdvance                     # /v1/now 不推进时钟
ok  hlc-service/server (race enabled)
```

覆盖率：

```bash
go test -coverprofile=cover.out ./...
# hlc-service/hlc     coverage: 78.4%
# hlc-service/server coverage: 86.7%
go tool cover -func=cover.out | tail -1
# total: (statements) 78.8%
```

未覆盖部分主要是：真正读墙钟的 `defaultPhysical`（测试刻意用假时钟替代）、
`main.go` 的进程引导/信号处理（薄胶合层）。核心算法 `merge` 100% 覆盖，
`Receive` 92.9%，HTTP `handleReceive/handleTick/handleStatus/handleHealth` 100%。

> 关键：**所有判定逻辑的测试都不读取真实时间**。`hlc` 包通过
> `WithPhysicalFunc`/`WithWaitFunc` 注入可控的假物理时钟，确定性地构造同毫秒
> 突发、回拨（1000→1500 倒退）、遥远未来值（2^62−1）、计数器打满等场景。

---

## 2. 真实 HTTP 端到端运行

构建并启动三个真实节点：

```bash
go build -o /tmp/hlc-server .
/tmp/hlc-server --addr=127.0.0.1:19190 --node=node-A --drift=1000 ...
/tmp/hlc-server --addr=127.0.0.1:19191 --node=node-B --drift=1000 ...
/tmp/hlc-server --addr=127.0.0.1:19192 --node=node-C --drift=9223372036854775807 --overflow-wait=100
HLC_NO_SPAWN=1 bash examples/demo.sh     # 退出码 0
```

实测结果摘录（完整输出即本节描述；时间戳为运行当时真实 Unix 毫秒）：

| # | 场景 | 实测结果 |
|---|------|----------|
| 1 | 健康检查 | `{"node_id":"node-A","status":"ok",...}` 200 |
| 2 | **同毫秒突发**：64 个并发 HTTP tick | 64 响应、**64 个唯一时间戳**、严格递增；最忙一毫秒承载 8 个事件，logical 连续 `0..7` |
| 3a | 跨墙钟因果链 A→B→A | 每跳时间戳严格增大 |
| 3b | **同毫秒因果合并**（注入超前本地约 400ms、在漂移上限内的时间戳） | physical 钉住 `1790213167803`，logical `10 → 接收后 11 → 本地 tick 12 → 再接收 13`，清晰展示逻辑计数爬升 |
| 4 | **回拨/巨大未来值**：超前 1 小时 | **422 future_drift**，错误信息给出实际超前毫秒数与上限；拒绝后时钟仍可用 |
| 5 | 边界：恰好超前 1000ms | **200 接受** |
| 6 | **大整数 JSON 无损**：`physical=1790213167990, logical=4294967293` | 响应 `...:4294967294`（+1），physical 逐位一致，断言通过 |
| 7 | `hlc://` 规范文本往返 | `"hlc://wire/1790213167991:4294967293"` 精确解析并回显 `...:4294967294` |
| 8 | **计数器溢出**：远端 logical 顶在上限且 physical 遥远 | **503 overflow，约 100ms 返回（受 --overflow-wait 限制），不挂起** |
| 9 | 状态计数 | `tick_count=65, receive_count=3, drift_rejects=1, overflow_waits=0, skew_ms=793` |

---

## 3. 开发过程中实际发现并修复的问题（如实记录）

1. **接收合并在溢出等待后可能进入错误分支（实现初版）**：早期 `Receive` 用一组
   临时 `switch` 分支，等待物理时间前进后重新循环的终止条件写得不对。已重构为
   单一纯函数 `merge(localPT, localLC, remote, p, maxLogical)`，按论文四象限返回
   `(newPT, newLC, overflow)`，由调用方处理溢出；`merge` 现 100% 测试覆盖。

2. **uint64 回绕（真实 bug，有回归测试）**：当远端 `logical = 2^64−1` 且其
   physical 为最大值时，合并需要 `logical+1`，朴素实现会**回绕成 0**，使
   "溢出检查"失效并可能发出倒退时间戳。改为 `merge` 显式比较 `base >= maxLogical`
   后报告 overflow，不依赖加法结果。回归测试
   `TestReceiveOverflowAtUint64LimitNoWraparound` /
   `TestHTTPJSONUint64MaxOverflowProvesWraparoundFixed`。

3. **溢出等待会无限阻塞（真实 HTTP 实测发现）**：端到端测试中，向一个放开漂移
   上限的节点发送"logical 顶满、physical 在遥远未来"的时间戳，请求**永久挂起**
   （旧 `defaultWait` 一直睡到那个遥远物理时间）。修复：`WaitFunc` 增加
   `deadline`，Clock 增加 `maxOverflowWait`（默认 250ms，可经
   `--overflow-wait` / `HLC_OVERFLOW_WAIT_MS` 配置），超时返回 `ErrOverflow`
   → HTTP 503。实测该请求从"永久挂起"变为约 100ms 返回 503，且服务继续可用。
   回归测试 `TestReceiveOverflowBoundedWaitDoesNotHang`。

4. **JSON 字符串形态的请求体解析顺序**：`"hlc://..."` 先被尝试解析成 `map` 而报
   400。已在 `decodeTimestamp` 中先处理 JSON 标量字符串，再处理对象。

---

## 4. 复现实验的命令

```bash
# 单元 + HTTP 测试（竞态检测）
go test -race -count=1 ./...

# 覆盖率
go test -coverprofile=cover.out ./... && go tool cover -func=cover.out

# 端到端演示（脚本自管节点；端口 19190-19192）
go build -o /tmp/hlc-server .
bash examples/demo.sh

# 若在不允许脚本派生后台进程的 shell 中，先手动启动三个节点，再：
HLC_NO_SPAWN=1 bash examples/demo.sh

# 手动请求样例
bash examples/requests.sh
```

---

## 5. 未完成项 / 已知限制（如实列出）

- **无持久化**：HLC 状态仅存内存，进程重启以当前墙钟初始化。论文模型本身面向
  运行期事件排序；跨重启的单调延续需要额外存储，本服务未实现（README"设计取舍"
  已说明）。
- **无鉴权 / TLS / 限流**：定位为本地/内网的后端原语，未加认证与传输加密；
  `/v1/tick` 也无限流（并发安全但不防滥用）。生产暴露前需在前置网关补齐。
- **漂移保护只覆盖接收路径**：本节点自身墙钟整体跑偏（系统时间被设错）超出服务
  职责，需要 NTP 等外部保障。
- **未做分布式/多实例一致性测试集群**：跨节点语义用 `httptest` 三服务器和真实
  三进程两种方式验证，但未在多机真实网络偏差下长时间浸泡。
- **`main.go` 进程引导层无自动化测试**：flag/env 解析与信号优雅关闭仅做了手动
  启动/`Ctrl-C` 验证，未纳入 `go test`。
- 环境备注：本机 `go` 不在默认 `PATH`（位于 `/usr/local/go/bin`）；运行测试时
  使用 `PATH=$PATH:/usr/local/go/bin`。端口 18080/18081 在机器上被无关进程占用，
  演示改用 19190–19192。
