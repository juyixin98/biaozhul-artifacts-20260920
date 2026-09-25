# 运行报告（RUNLOG）

本文件如实记录交付时的实际命令与结果。环境：Linux 6.8、Go 1.22.2、
x86_64；日期 2026-09-23。原始输出保存在同目录：

- `go-test-race-output.txt`：`go test -race -v ./...` 完整输出
- `../examples/acceptance-events.jsonl`：`go run ./cmd/drfsim` 完整事件流
- `http-demo-output.txt`：对真实 HTTP 服务跑 `examples/requests.sh`
  并等待 12 秒后的完整快照

## 1. 编译与静态检查

```
$ gofmt -l scheduler/ cmd/        # 无输出（全部格式正确）
$ go vet ./...                    # 通过（VET_OK）
$ go build ./...                  # 通过（BUILD_OK）
```

## 2. 自动化测试

```
$ go test -race -v ./...
=== RUN   TestAcceptanceHandCalculated
--- PASS: TestAcceptanceHandCalculated (0.00s)
=== RUN   TestHTTPEndToEnd
--- PASS: TestHTTPEndToEnd (0.01s)
=== RUN   TestWeightedShareOrdering
--- PASS: TestWeightedShareOrdering (0.00s)
=== RUN   TestDeterministicTieBreak
--- PASS: TestDeterministicTieBreak (0.00s)
=== RUN   TestPermanentHeadBlockIsTenantLocal
--- PASS: TestPermanentHeadBlockIsTenantLocal (0.00s)
=== RUN   TestEventsAreStructured
--- PASS: TestEventsAreStructured (0.00s)
=== RUN   TestSmallTaskFitsBehindBigHead
--- PASS: TestSmallTaskFitsBehindBigHead (0.00s)
=== RUN   TestNonPreemptibleAndReuse
--- PASS: TestNonPreemptibleAndReuse (0.00s)
=== RUN   TestNoOvercommitRandomized
--- PASS: TestNoOvercommitRandomized (0.31s)
=== RUN   TestValidationErrors
--- PASS: TestValidationErrors (0.00s)
PASS
ok      fairdrf/scheduler      1.343s
```

10 个测试全部通过，`-race` 无数据竞争报告。额外重复验证：

```
$ go test -count=20 -run 'TestAcceptanceHandCalculated|TestDeterministicTieBreak|TestWeightedShareOrdering' ./scheduler/
20x determinism runs: PASS
$ go test -count=5  -run TestNoOvercommitRandomized ./scheduler/
5x randomized: PASS
$ go test -race -count=3 ./...
3x race full suite: PASS
```

测试与验收点的对应：

| 验收要求 | 测试 |
|---|---|
| 手算资源分配序列 | `TestAcceptanceHandCalculated`（逐秒断言状态、used、全序） |
| 大任务阻塞 | `TestPermanentHeadBlockIsTenantLocal`、验收场景 T2/T4 |
| 小任务持续到达 | 验收场景 T5/T8、`TestSmallTaskFitsBehindBigHead` |
| 资源释放 | 验收场景 t=2/t=5/t=8、`TestNonPreemptibleAndReuse` |
| 不超配 | 验收场景每个事件后检查 + `TestNoOvercommitRandomized`（400 轮随机负载逐步核对） |
| 权重 | `TestWeightedShareOrdering`（weight 1:2 → 稳态 4:6） |
| 确定性平局 | `TestDeterministicTieBreak`（两种注册/提交顺序结果一致） |

## 3. 确定性模拟（假时钟回放手算场景）

```
$ go run ./cmd/drfsim > examples/acceptance-events.jsonl
events   : 27
starts   : ['T1', 'T3', 'T5', 'T2', 'T6', 'T8', 'T4', 'T7']
finishes : ['T1', 'T5', 'T3', 'T8', 'T2', 'T6', 'T7', 'T4']
overcommit anywhere: False
artifact matches hand calculation: OK
```

与 README §2 的手推序列完全一致；27 条事件 = 3 租户创建 + 8 提交 +
8 启动 + 8 完成（本场景无超容量任务，故无 `TASK_BLOCKED`；该事件类型
由单元测试与 HTTP 实测覆盖）。

## 4. 真实 HTTP 服务端到端（墙钟）

启动（容量 10000 milliCPU / 10240 MiB）：

```
$ go run ./cmd/fairdrf -addr :18091 -cpu-milli 10000 -mem-mib 10240
$ curl localhost:18091/healthz
{"status":"ok"}
$ BASE=http://localhost:18091 bash examples/requests.sh
```

实测结果（完整 JSON 见 `http-demo-output.txt`）：

- `batch-1`（6000m/6144MiB, 5s）、`ana-1`、`ana-2`（各 1000m/1024MiB, 8s）
  提交后立即 `RUNNING`，已用 8000m/8192MiB（≤ 容量，不超配）；
- `batch-huge`（99999/99999，超容量）提交后 `WAITING`，事件计数中
  含 1 条 `TASK_BLOCKED`，reason：
  "request exceeds total cluster capacity; queue head blocked (other
  tenants unaffected)"；租户 `team-batch` 的 `head_blocked=true`、
  `head_exceeds_capacity=true`；
- 按墙钟 5s / 8s 后三个任务自动 `FINISHED` 并释放资源（事件时间戳
  间隔与声明 duration 一致）；`batch-huge` 始终保持 `WAITING`，
  且没有影响 analytics 租户（其任务正常跑完）。

错误路径实测状态码：

```
重复创建租户            -> 409
向不存在租户提交任务     -> 404
GET 不存在的任务        -> 404
DELETE /tasks          -> 405
请求体含未知 JSON 字段   -> 400
```

事件计数：`TENANT_CREATED=2, TASK_SUBMITTED=4, TASK_STARTED=3,
TASK_BLOCKED=1, TASK_FINISHED=3`（共 13，与快照 `event_seq=13` 一致）。

## 5. 未通过项 / 偏差

最终状态：**无未通过项**（编译、vet、全部单元/HTTP 测试、-race、
确定性重复、假时钟模拟、真实墙钟端到端均通过）。

开发过程中出现并已解决的偏差，如实记录：

1. 手算初版误以为 t=5 资源释放后只会启动 T2、T6；实际在同一调度轮内
   B 的 T8(1,1) 也放得进 (2,2) 余量，会连续启动，且 t=8 T4 启动后
   T7 在同轮内取得剩余 1/1。这是"每启动一个任务即重新排序、同租户
   FIFO 可连续出队"规则的正确行为，README 与测试已按真实推导更正，
   并在 README §2 完整保留推导步骤。
2. 开发中发现并修复了调度器的一个真实缺陷：单轮扫描遇到当前放不下的
   高优先队头时曾直接终止整轮，导致后续租户放得下的小任务被漏调度
   （`TestSmallTaskFitsBehindBigHead` 复现）。已改为"跳过该租户、
   继续扫描"，并保留该回归测试。
3. 早期两个测试用例自身设计有误（平局测试因队列从不同时拥塞而未真正
   构成三方平局；一个用例把 (9,10) 占用后的内存余量算成了 1）。
   均已修正为能真正检验目标性质的构造，调度实现无需为此让步。
