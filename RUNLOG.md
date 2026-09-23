# RUNLOG — 实际运行记录

- 日期：2026-09-24
- 环境：Linux 6.8.0-90-generic (amd64)，Go 1.22.2，单机本地回环
- 方式：所有命令均在项目根目录实际执行；以下为真实输出（JSON 查询结果做了
  省略排版，命令与结论未改写）。

## 1. 构建与静态检查

```
$ go build ./...
$ go vet ./...
$ gofmt -l .
（无输出，全部文件格式干净）
```

## 2. 单元测试

首次运行出现过 1 个失败（已定位并修复，如实记录）：

```
$ go test ./...
--- FAIL: TestTimeoutFlushesComplete (0.00s)
    merger_test.go:137: flushed 0, want 1
FAIL    logpipe/internal/merger
```

原因：测试自身的假时钟推进算错——起始行 10:00:00、续行 10:00:01 后只把
时钟推到 10:00:02，空闲仅 1s，未达到 2s 超时阈值。修正测试时钟推进
（再前进 2s 使空闲达到阈值）后：

```
$ go test ./... -count=1
?       logpipe/cmd/server      [no test files]
?       logpipe/cmd/simulator   [no test files]
ok      logpipe/internal/merger 0.007s
ok      logpipe/internal/server 0.078s
ok      logpipe/internal/store  0.009s
```

20 个测试用例（`go test -v` 统计），全部通过，覆盖：

- 起始行组装、`next_start` 闭合、超时闭合；
- 两来源逐行交错、断言无跨来源串行；
- 无起始行孤儿条目；
- pid 变化重启（续行到达 / 起始行到达两种情形）；
- 单行超字节预算截断、溢出后续行丢弃计数；
- Close 刷 `shutdown`、ForceFlush 刷 `forced`；
- 非法输入拒绝；
- 8 来源 × 200 条并发摄入（配合竞态检测）；
- JSONL 重放、id 续号、末尾半行容忍；
- HTTP 层：单对象/数组摄入、400 校验、force-flush、超时扫描、
  交错来源查询隔离、healthz。

竞态检测：

```
$ go test -race -count=1 ./...
ok      logpipe/internal/merger 1.060s
ok      logpipe/internal/server 1.101s
ok      logpipe/internal/store  1.023s
```

## 3. 端到端：真实 HTTP 服务 + 合成数据自验证

构建二进制并启动（用短超时便于观察；注：环境中 18080/18091 端口已被
其他进程占用，`bind: address already in use`，改用内核分配的空闲
临时端口 54067）：

```
$ go build -o bin/logpipe ./cmd/server && go build -o bin/simulator ./cmd/simulator
$ ./bin/logpipe -addr 127.0.0.1:54067 -data-dir /tmp/logpipe-demo/data \
    -timeout 1s -sweep-interval 100ms
logpipe: store ready at /tmp/logpipe-demo/data (0 entries replayed)
logpipe: listening on 127.0.0.1:54067 (start rule "^\\d{4}-...", timeout 1s, max-bytes 4096)
$ curl -s http://127.0.0.1:54067/healthz
{"status":"ok"}
```

运行验收模拟器（交错两来源堆栈 + 无起始行 + 超长 + 重启）：

```
$ ./bin/simulator -url http://127.0.0.1:54067 -wait 2s -max-bytes 4096
simulator: all lines posted; waiting 2s for timeout flushes...
simulator: got 10 assembled entries

PASS  alpha stack A (interleaved, closed by B)   id=2 lines=3 bytes=142 reason=next_start
PASS  alpha stack B (interleaved, closed by C)   id=3 lines=2 bytes=107 reason=next_start
PASS  alpha stack C (interleaved, timeout tail)  id=6 lines=2 bytes=109 reason=timeout
PASS  beta stack D (interleaved, closed by E)    id=1 lines=2 bytes=95  reason=next_start
PASS  beta stack E (interleaved, timeout tail)   id=7 lines=4 bytes=178 reason=timeout
PASS  gamma orphan (no start line)               id=4 lines=1 bytes=51  reason=orphan
PASS  gamma stack G (timeout)                    id=8 lines=2 bytes=111 reason=timeout
PASS  delta overflow (single oversized line)     id=9 lines=1 bytes=4096 reason=bytes_limit
PASS  epsilon old entry (process restart)        id=5 lines=2 bytes=110 reason=restart
PASS  epsilon new entry (post-restart, timeout)  id=10 lines=2 bytes=109 reason=timeout

ALL CHECKS PASSED: 10 entries, 10 scenarios, no cross-source mixing
exit=0
```

模拟器对每个条目都断言了 source、complete、reason、line_count；超长条目
额外断言 `truncated=true`、`bytes<=4096`、`dropped_lines>=1` 且溢出后
续行文本不在条目内；并对每个条目扫描全部 10 个来源标记，确认没有任何
外来标记（无跨来源混合的端到端证据）。

## 4. 优雅关停刷写

向运行中的服务 POST 一条挂起条目（未超时），随即 SIGTERM：

```
$ kill -TERM <pid>
logpipe: shutdown signal received, draining HTTP traffic
logpipe: stopped cleanly
$ grep shutdown-probe /tmp/logpipe-demo/data/entries.jsonl | python3 -m json.tool
{
  "id": 11,
  "source": "shutdown-probe",
  "text": "2026-09-24T10:00:00 pending header\n    pending frame",
  "line_count": 2, "bytes": 52,
  "complete": false, "reason": "shutdown"
}
```

JSONL 共 11 行，关停前挂起的条目以 `shutdown` 不完整落盘，无丢失。

## 5. 进程重启后持久化与查询

同一 `-data-dir` 重新启动：

```
logpipe: store ready at /tmp/logpipe-demo/data (11 entries replayed)

$ curl -s "http://127.0.0.1:<port>/entries?source=alpha"
count = 3
  id=2  alpha  complete=true   reason=next_start
  id=3  alpha  complete=true   reason=next_start
  id=6  alpha  complete=true   reason=timeout

# 重启后新写入的条目 id 续号
id = 12 | reason = timeout | complete = true
```

## 6. 请求样例文件实跑

```
$ curl -s -X POST .../ingest --data @examples/ingest-single.json
{"accepted":1}
$ curl -s -X POST .../ingest --data @examples/ingest-batch.json
{"accepted":8}
$ curl -s -X POST .../ingest --data @examples/ingest-restart.json
{"accepted":4}

$ curl -s .../entries   （实得 7 条，按 reason 列示）
id= 1 src=svc-orders   lines=1 complete=True  reason=next_start   # 与单条样例跨请求拼成同一堆栈
id= 2 src=svc-edge     lines=1 complete=False reason=orphan
id= 3 src=svc-restart  lines=2 complete=False reason=restart
id= 4 src=svc-edge     lines=2 complete=True  reason=timeout
id= 5 src=svc-restart  lines=2 complete=True  reason=timeout
id= 6 src=svc-orders   lines=3 complete=True  reason=timeout
id= 7 src=svc-payments lines=2 complete=True  reason=timeout
```

超长请求（6000+ 字节单行，预算 4096）：

```
id=8 src=svc-huge line_count=1 bytes=4096 complete=false
     reason=bytes_limit truncated=true dropped_lines=1
```

## 7. 一键演示脚本

```
$ ./scripts/demo.sh
...
ALL CHECKS PASSED: 10 entries, 10 scenarios, no cross-source mixing
entries visible after restart: 10
demo finished OK; data kept at /tmp/tmp.w5TUVXH35A/logpipe-demo
$ echo $?
0
```

（首版脚本退出码为 143：trap 中 `kill ${PID:-0}` 在变量为空时对当前
进程组发了信号，误杀脚本自身；已改为仅对非空 PID 发信号，重跑退出码 0。）

## 8. 未通过项 / 已知限制

- 最终代码：**无失败用例、无已知功能缺陷**；`go vet` 干净、
  `go test -race` 通过。
- 过程中出现并已修复的 2 个问题：见第 2 节（测试时钟算错）与
  第 7 节（演示脚本 trap 误杀自身）；另有 2 次启动因环境中
  18080/18091 端口被占用而失败，属环境问题，换临时端口后正常。
- 样例范围限制（非缺陷，README 已声明）：单文件 JSONL 无分片/压缩；
  查询无分页，只有 source 精确过滤 + 尾部 limit；无鉴权；超时刷出为
  轮询，实际时间为“超时 + 至多一个扫描间隔”。
- 字节截断最初按字节边界（可能切断多字节字符），已在交付前改为
  UTF-8 rune 安全截断并补充 `TestTruncateUTF8NeverSplitsRune`
  （构造 3 字节“中”文字符在预算边界被切的场景）。
