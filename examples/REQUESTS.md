# 请求样例与实跑记录

所有命令均在本机实跑（Go 1.22.2，linux/amd64）。演示服务只监听回环地址，
四层调用全部是本进程 loopback HTTP，叶子是脚本化假服务，**不访问外网**。

结构化 JSON 原始快照在 [`sample-output/`](sample-output/)。

## 0. 启动服务

```bash
go run ./cmd/server -addr 127.0.0.1:8377
# 2026/... retry-budget demo listening on http://127.0.0.1:8377
```

> 若 8080/8099 已被占用（本机实测两者都被别的进程占用），换用空闲端口，
> 下文以 `$PORT` 表示。

## 1. 健康检查与接口清单

```bash
$ curl -s http://127.0.0.1:$PORT/healthz
ok

$ curl -s http://127.0.0.1:$PORT/
{
  "service": "retrybudget",
  "endpoints": [
    {"method": "GET", "path": "/healthz", ...},
    {"method": "GET", "path": "/suite", ...},
    {"method": "GET", "path": "/mesh/{preset}?max=&deadline_ms=", ...}
  ]
}
```

## 2. 结构化验收套件（虚拟时钟，瞬时完成）

```bash
$ curl -s http://127.0.0.1:$PORT/suite | jq '.summary,
    [.cases[] | {case, status: .outcome.status, used: .outcome.total_used,
                 max: .outcome.root_max, tiers: .outcome.tiers, passed}]'
```

实跑结果（原始：`sample-output/suite.json`）：

```json
{"all_passed": true, "passed": 7, "total": 7}
```

| case | status | used/max | client/edge/mid/leaf |
|---|---|---|---|
| root-budget-cap | budget-exhausted | 8/8 | 1/1/3/3 |
| local-cap-upstream-retry | budget-exhausted | 12/12 | 2/2/4/4 |
| server-retry-after-recovery | success | 6/10 | 1/1/2/2 |
| retry-after-vs-deadline | deadline-exceeded | 4/20 | 1/1/1/1 |
| client-cancellation | canceled | 6/100 | 1/1/2/2 |
| non-retryable-400 | non-retryable | 4/10 | 1/1/1/1 |
| immediate-success | success | 4/10 | 1/1/1/1 |

每行均满足 `client+edge+mid+leaf == used <= max`。

## 3. 实时调用：根预算上限（真实 HTTP + 真实退避）

```bash
$ curl -s "http://127.0.0.1:$PORT/mesh/budget-cap?max=8" | jq .result
{
  "http_status": 503,
  "verdict": "budget-exhausted",
  "total_used": 8,
  "root_max": 8,
  "attempts": 1,
  "error": "client: retry budget exhausted ... (used=8)"
}
```

时间线（节选，`elapsed_ms` 为真实墙钟毫秒）展示全局尝试号 1→8：

```
  0.0ms client attempt-start a=1 call edge (used=1/8)
  0.2ms edge   attempt-start a=2 forward -> mid (used=2/8)
  0.5ms mid    attempt-start a=3 forward -> leaf (used=3/8)
  0.8ms leaf   attempt-end   a=4 script hit 1 -> 503
 81.2ms mid    attempt-start a=5 ...
 81.4ms leaf   attempt-end   a=6 script hit 2 -> 503
281.9ms mid    attempt-start a=7 ...
282.1ms leaf   attempt-end   a=8 script hit 3 -> 503
282.3ms edge   attempt-end   a=2 verdict=budget-exhausted used=8
282.4ms client attempt-end   a=1 verdict=budget-exhausted used=8
```

三层都愿意重试，但总尝试数被根预算**精确**钉在 8。

## 4. 实时调用：服务端 Retry-After（真实等待约 1 秒）

```bash
$ curl -s "http://127.0.0.1:$PORT/mesh/retry-after" | jq .result
{
  "http_status": 200,
  "verdict": "ok",
  "total_used": 6,
  "root_max": 10,
  "attempts": 1,
  "error": ""
}
```

时间线关键两行：第一次叶子 `429` 带 `Retry-After: 1`，mid 真正等待
`1000ms` 后第二次成功：

```
  0.9ms mid attempt-end a=3 ... retry-after 1s
1001.0ms mid attempt-start a=5 forward -> leaf
1001.2ms leaf attempt-end a=6 script hit 2 -> 200 ok
```

## 5. 实时调用：Retry-After 与截止时间冲突

```bash
$ curl -s "http://127.0.0.1:$PORT/mesh/deadline" | jq .result
# preset: 叶子 429 + Retry-After 10s，根 deadline=400ms
{
  "http_status": 504,
  "verdict": "deadline",
  "total_used": 4,
  "root_max": 20,
  ...
}
```

不会傻等 10 秒：退避被裁剪到剩余截止时间，随后以 deadline 终态结束。

## 6. 实时调用：确定性错误绝不重放

```bash
$ curl -s "http://127.0.0.1:$PORT/mesh/non-retryable" | jq .result
{
  "http_status": 400,
  "verdict": "non-retryable",
  "total_used": 4,
  "root_max": 10,
  ...
}
```

每个层恰好一次（一条链路共 4 个槽位），400 不被重放。

## 7. 实时调用：健康路径

```bash
$ curl -s "http://127.0.0.1:$PORT/mesh/success" | jq .result
{
  "http_status": 200,
  "verdict": "ok",
  "total_used": 4,
  "root_max": 10,
  "attempts": 1,
  "error": ""
}
```

## 8. 取消（由自动化测试覆盖）

取消在真实墙钟下触发，无法用一条 curl 复现（请求很快返回）。对应验收：

```bash
$ go test ./e2e/ -run TestCancellationUnwindsAllLayers -v
```

场景：根预算 100，40ms 后 `cancel()`；实测终态 `canceled`，远未触及预算
上限（本次记录为 client1/edge1/mid2/leaf2，共 6 槽；具体数随墙钟调度略有
浮动），`sum == used` 记账守恒。

## 9. 未知 preset

```bash
$ curl -i http://127.0.0.1:$PORT/mesh/nope
HTTP/1.1 404 Not Found
{"error":"unknown preset","available":"budget-cap, retry-after, deadline, non-retryable, success"}
```

## 10. 自动化测试命令

```bash
$ go test ./... -race -count=1
ok  	retrybudget/cmd/server
ok  	retrybudget/e2e
ok  	retrybudget/internal/clock
ok  	retrybudget/internal/demo
ok  	retrybudget/internal/fault
ok  	retrybudget/internal/mesh
ok  	retrybudget/internal/propagation
ok  	retrybudget/internal/retry

$ go test ./... -coverpkg=./... -coverprofile=c.out -covermode=atomic
$ go tool cover -func=c.out | tail -1
total: (statements) 87.1%
```
