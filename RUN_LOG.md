# 运行记录（如实）

环境：Linux 6.8.0-90-generic x86_64，Go go1.22.2，单机本机。日期：2026-09-25。
所有“外部依赖”均为本进程假服务，未访问任何生产系统。下列命令均在仓库根目录实际执行。

## 1. 构建与静态检查

```bash
$ go build ./... && go vet ./...
BUILD_OK
$ gofmt -l .
（无输出 = 全部已格式化）
```

构建为单个二进制：

```bash
$ go build -o bin/canceltree ./cmd/canceltree
$ ls -la bin/canceltree
-rwxrwx-r-x 1 admin admin 8417135 Sep 25 18:41 bin/canceltree
```

## 2. 自动化测试

全量 + 竞态检测（最终结果）：

```bash
$ go test ./... -race -count=1 -timeout 180s -cover
?  	canceltree/internal/testutil	[no test files]
ok  canceltree/cmd/canceltree	1.031s	coverage: 52.4% of statements
ok  canceltree/internal/client	1.072s	coverage: 90.6% of statements
ok  canceltree/internal/clock	1.017s	coverage: 82.0% of statements
ok  canceltree/internal/server	1.456s	coverage: 82.8% of statements
ok  canceltree/internal/tree	1.224s	coverage: 93.0% of statements
ok  canceltree/internal/upstream	1.075s	coverage: 82.7% of statements
```

取消/竞争用例重复 10 轮（稳定，无 flake、无 data race）：

```bash
$ go test ./internal/tree/ -race -count=10 -timeout 300s
ok  canceltree/internal/tree	3.141s
```

全量重复 5 轮同样全绿：

```bash
$ go test ./... -race -count=5 -timeout 300s
ok  canceltree/internal/client ... /clock ... /server ... /tree ... /upstream
```

## 3. 真实启动服务的端到端验证（curl）

> 注：首次尝试 `-addr 127.0.0.1:18080` 时，该端口已被机器上一个无关进程
> （pid=513019 的 `server`）占用，程序如实报错退出：
> `listen tcp 127.0.0.1:18080: bind: address already in use`。改用 18091 端口成功。
> 这也验证了监听失败会被正确返回（并有 `TestRunListenerFailureReported` 单测覆盖）。

```bash
$ ./bin/canceltree -addr 127.0.0.1:18091
$ curl -s localhost:18091/healthz -o /dev/null -w "%{http_code}\n"
200
```

### 3.1 全部成功

```bash
$ curl -s -X POST localhost:18091/api/v1/process -d @examples/01-success.json ...
status=succeeded；两个子任务 200，elapsed_ms≈41（并行，取最慢者）
```

### 3.2 首个致命失败取消其余任务，但清理全部执行

```bash
$ curl -s -X POST localhost:18091/api/v1/process -d @examples/02-fatal-cancels-others.json
status: failed | fatal: payment-authorize            HTTP 503
  slow-downstream-a    canceled  by=fatal-sibling  cleanup_ran=true
  slow-downstream-b    canceled  by=fatal-sibling  cleanup_ran=true
  payment-authorize    failed                       cleanup_ran=true
```

### 3.3 非致命失败不取消兄弟

```
status: failed
  optional-enrichment  failed   http=503
  core-work            succeeded http=200
```

### 3.4 子任务超时（hold 永不释放，timeout_ms=100）

```
status: failed（HTTP 503）；task=canceled by=task-timeout；cleanup_ran=true；elapsed_ms=100
```

### 3.5 客户端故障：延迟 与 连接重置

```
status: failed
  slow-client-side   succeeded http=200 err=''
  connection-reset   failed    http=0  err='transport error: Get ".../upstream/reset": EOF'
```

### 3.6 客户端断开终止剩余计算（核心验收）

使用 `examples/05-client-stall-fault.json`：一个 `stall` 任务（本地阻塞、永不发请求、
只响应 ctx 取消）+ 一个 hold 兄弟。正常情况下请求永不返回；用 `--max-time 0.3`
模拟客户端断开：

```bash
$ curl ... -d @examples/05-client-stall-fault.json --max-time 0.3
curl: (28) Operation timed out ...   # 客户端侧拿到断连/超时
# 服务端日志：
request sample-client-faults: client disconnected before response completed; cleanups still ran
# /api/v1/diagnostics 断开前后对比：
#   upstream.canceled  3 -> 4   （+1，断开传递到了在途调用）
#   upstream.cleanups  4 -> 6   （+2，两个子任务清理都执行）
#   requests.canceled  0 -> 1
#   connections.in_flight = 0，upstream.inflight = 0，active_holds = 0
```

### 3.7 重复断开不泄漏（goroutine / 连接 / 内存）

连续 10 轮“3 个 hold 子任务 + 0.2s 后客户端断开”：

```
after 10 real disconnect rounds:
  inflight=0  active_holds=0  in_flight=0  open(idle)=3  total_established=46
  goroutines=14
  requests={total:14, succeeded:0, failed:3, canceled:11}
```

`open(idle)=3` 稳定在连接池上限、不随 10 轮增长；在途调用/在途上游请求/活跃 hold 全部为 0；
goroutine 数稳定。更严格的“连接数严格归零”断言由带 `DisableKeepAlives` 的自动化测试
（`TestNoLeakAcrossRepeatedDisconnects` 等）在 `-race` 下完成，并额外比较强制 GC 前后的
goroutine 数与堆分配（预算内）。

### 3.8 优雅关停

```bash
$ curl -s -X POST localhost:18091/shutdown
{"status":"shutting down"}      HTTP 202
# 日志：... stopped
$ curl localhost:18091/healthz
000（已下线）
```
`SIGTERM` 关停路径同样由 `cmd/canceltree` 的冒烟测试覆盖。

## 4. 开发过程中遇到并已修复的问题（如实记录）

1. **假服务无 hold 且 delay=0 时永久挂起。** 初版在 select 中放入了 `time.NewTimer(0)`
   的 C，加上 nil 的 release channel，三个 case 实际都不可用，导致普通快速调用不返回
   （表现为 `TestWorkFailInjection` 挂起、全包超时）。修复：仅 delay>0 时才创建定时器，
   且“没有任何可等待条件”时跳过 select 立即响应。
2. **取消语义与假时钟初版不一致。** 早期时钟只有 `After`，无法 Stop，假时钟会残留定时器、
   不利于泄漏判定；重构为可 `Stop()` 的 `Timer` 接口，停止即从假时钟摘除。
3. **连接“泄漏”实为 keep-alive 空闲池。** 端到端断言连接数归零失败（`Active=2`）。
   根因是生产客户端复用连接，完成的连接按设计留在有界空闲池中。修复：区分
   `open`（含空闲池）与 `in_flight`（真正在途）两个指标，并增加 `DisableKeepAlives`
   测试缝以支持“严格归零”断言；README“泄漏口径”一节明确两种口径。
4. **任务自身超时却返回 200。** 初版最终分类只把 `StatusFailed` 计入失败，超时任务状态为
   `canceled/by=task-timeout`，导致整树误报 succeeded。修复：最终分类把任务超时一并判为
   失败（503），并补充对应断言。
5. **一个手写用例的占位代码与无意义断言**（如 `errors.Is(context.Canceled, context.Canceled)`）
   在编写中途被发现并删除/替换为真实断言。

## 5. 未通过项 / 已知限制

- 最终交付状态下，`go vet`、`gofmt`、全部单测/端到端测试（`-race`、多轮重复）均通过，
  **无未通过项**。
- 已知限制（设计取舍，非缺陷）：
  - 假上游是**同进程** HTTP 而非跨主机部署；
  - 生产配置保留有界 keep-alive 空闲连接池，因此“原始 TCP 连接数”在池中空闲超时前不严格为 0
    （但有界、不随负载持续增长）；需要严格归零口径时使用 `DisableKeepAlives`；
  - 清理为单次有界调用，不做持久化重试/清理队列；
  - 无前端（按需求刻意为之）。
