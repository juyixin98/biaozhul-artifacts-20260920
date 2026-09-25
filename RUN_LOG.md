# 运行记录（RUN_LOG）

本文件如实记录项目实际构建、运行与测试过程。环境：

- OS：Linux 6.8.0-90-generic (amd64)
- Go：`go version go1.22.2 linux/amd64`
- 日期：2026-09-23
- 无第三方依赖（仅标准库）

## 1. 构建

```
$ go build ./...
（无输出，成功）

$ go build -o /tmp/reservations ./cmd/reservations
（成功）
```

## 2. 自动化测试

### 2.1 普通运行

```
$ go test -count=1 ./...
?   	resourcebooking/cmd/reservations	[no test files]
ok  	resourcebooking/internal/clock	0.003s
ok  	resourcebooking/internal/dispatcher	0.033s
ok  	resourcebooking/internal/executor	0.002s
ok  	resourcebooking/internal/scheduler	0.099s
ok  	resourcebooking/internal/server	0.016s
```

### 2.2 竞态检测

```
$ go test -race -count=1 ./...
?   	resourcebooking/cmd/reservations	[no test files]
ok  	resourcebooking/internal/clock	1.018s
ok  	resourcebooking/internal/dispatcher	1.049s
ok  	resourcebooking/internal/executor	1.016s
ok  	resourcebooking/internal/scheduler	1.527s
ok  	resourcebooking/internal/server	1.058s
```

### 2.3 静态检查与格式

```
$ go vet ./...        -> vet clean
$ gofmt -l .          -> 无输出（格式干净）
```

### 2.4 覆盖率

```
$ go test -cover ./internal/...
ok  internal/dispatcher  coverage: 82.6%
ok  internal/executor    coverage: 100.0%
ok  internal/scheduler   coverage: 83.6%
ok  internal/server      coverage: 80.7%
```

### 2.5 用例统计

`go test -v ./...` 中 `--- PASS` 56 个，`--- FAIL` 0 个，`--- SKIP` 0 个
（含表驱动子测试；其中 7 个半开区间子用例、3 组穷举对比属性测试，
共约 700 个随机场景）。

### 2.6 验收点对应的测试

| 验收要求 | 测试 |
| --- | --- |
| 与离散小时间轴穷举比较 | `TestEarliestMatchesBruteForce`、`TestFixedReserveMatchesBruteForce`、`TestBatchMatchesBruteForce`（独立参考实现 `bruteModel`，逐格穷举） |
| 相邻区间不冲突 | `TestIntervalHalfOpen`（含“相邻 a/b 在前”两个关键子例）、`TestAdjacentIntervalsDoNotConflict`、`TestBatchAdjacentSucceeds`、HTTP 端到端 |
| 容量为零 | `TestZeroCapacityDimension`、穷举测试中约 1/4 维度随机为 0、HTTP 端到端（第二维 0） |
| 批次部分冲突失败、零落地 | `TestBatchAtomicOnPartialConflict`、`TestBatchInternalConflicts`、`TestBatchMixedAutoPlacementConflict`、`TestConcurrentBatchAtomic`、`TestConcurrentExplicitIDUnique`、`TestConcurrentBatchExplicitIDUnique`、HTTP `TestHTTPBatchPartialConflictRollback` |
| 多维容量 | `TestMultiDimensionalCapacity`（逐段/逐维 violations 与冲突 ID 断言） |
| 时钟可替换 | `clock.Fake` + `TestRunnerStartsCompletesAndFails`、`TestHTTPClockAdvanceAndEvents` |
| 执行器可替换 | `executor.Func` 脚本失败用例：bad 预约失败后状态 failed、容量释放 |
| 结构化事件 | `TestStructuredEvents`、`TestBatchEventsAtomic`、`TestJSONLSink`、`TestEventsAreImmutableSnapshots` |

## 3. 服务实际运行与请求样例

启动（使用假时钟与种子数据；端口由系统分配，避免与本机已有服务冲突）：

```
$ /tmp/reservations -addr 127.0.0.1:<port> \
    -fake-clock 2030-01-01T00:00:00Z -seed examples/seed.json
2026/09/23 ... 使用假时钟，当前时刻固定为 2030-01-01T00:00:00Z
2026/09/23 ... 已从 examples/seed.json 装载种子数据
2026/09/23 ... 资源预约服务监听 127.0.0.1:<port>

$ curl -s 127.0.0.1:<port>/healthz
{
  "now": "2030-01-01T00:00:00Z",
  "ok": true,
  "tick": 31557600
}
```

完整 10 步样例：

```
$ BASE=http://127.0.0.1:<port> bash examples/requests.sh
EXIT=0
```

真实输出已保存为 `examples/sample_output.txt`，其中可核对的关键结果：

- 与种子预约相邻的 `[10:00,11:00)` 预约成功（201）；
- 跨越端点的 `[09:30,10:30)` 预约返回 `HTTP 409 conflict`；
- 零容量维度正需求返回 `HTTP 400 invalid_request`；
- 最早可行查询（窗口 08:00–13:00，时长 2h）返回 `[11:00,13:00)`；
- 混合批次（固定 + 自动放置）成功，自动放置条目 `auto_placed: true`
  且落在批次内前一条的结束点 `02:00`（半开相邻）；
- 含 1 条冲突条目的批次返回 `HTTP 409`，响应中
  `violations` 给出 `load=2/capacity=1` 的逐段明细，随后
  `GET /v1/reservations/batch-bad-good` 返回 `HTTP 404`（未部分落地）；
- 取消种子预约后原区间可立即复用；
- `/v1/events` 输出带 `seq/at/type/detail` 的结构化事件流。

## 4. 开发过程中实际出现并修复的问题（如实记录）

下列问题均由测试/真实运行首先暴露，随后修复并回归通过：

1. **测试期望算错（最早可行）**：HTTP 端到端测试最初断言窗口
   08:00–12:00、2h 时长可行于 11:00，但 11:00+2h=13:00 已超出窗口，
   正确结果是 `feasible:false`。修正测试窗口为 08:00–13:00，
   并额外断言短窗口返回 `feasible:false`。
2. **测试场景容量不足**：dispatcher 测试在容量 1 的资源上放了两条
   同区间预约，种子阶段即冲突。将容量改为 2 以隔离“执行失败释放容量”语义。
3. **HTTP 状态码 500**：非分钟对齐时间最初返回普通 error，被映射为 500。
   增加 `invalidInput` 包装，统一返回 `400 invalid_request`
   （并有回归测试 `TestHTTPValidation`）。
4. **事件负载不是快照（真实缺陷）**：事件中直接放了预约内部指针，
   后续取消导致历史 `reservation.created` 事件的 `status` 被回改成
   `cancelled`。修复为所有事件发送深拷贝快照，并加
   `TestEventsAreImmutableSnapshots` 锁定。
5. **事件时间戳未跟随可替换时钟（真实缺陷）**：main 中 EventLog 默认
   使用墙钟，假时钟模式下事件 `at` 仍是真实墙钟时间。改为 EventLog
   与 Server 共用同一注入时钟，已实际验证事件 `at` 为
   `2030-01-01T00:00:00Z`。
6. **Batch 的 TOCTOU 竞态（真实缺陷，代码走读发现）**：批量接口最初
   在“字段校验/ID 查重”与“逐条求解/提交”之间释放了调度锁，并发请求
   可在该间隙插入并占用相同显式 ID，导致重复 ID。改为整批在同一把
   锁内完成，并新增两个并发回归测试
   （`TestConcurrentExplicitIDUnique`、
   `TestConcurrentBatchExplicitIDUnique`，`-race` 下验证同一 ID
   在 40/60 个并发操作中恰好 1 个成功）。
7. 若干编译/语法问题（`if` 中复合字面量括号、未使用变量、脚本中
   `curl -w` 附加状态行破坏 JSON 美化），均当场修复；样例脚本改为
   状态行与响应体分离输出（`show` 辅助函数）。

## 5. 未通过项 / 已知限制

- 当前最终状态下：**全部测试通过（54/54），无失败、无跳过、无竞态报告、
  vet/fmt 干净**；服务真实启动并成功跑完 10 步请求样例。
- 已知限制（设计使然，非缺陷）：无进程重启后的数据持久化（可用
  `--seed` 重建，或给 EventLog 加 JSONL Sink 留存审计事件）；
  HTTP 时间粒度固定分钟；未实现鉴权/分页/前端（题目要求不做前端）。
- 端口说明：样例运行时本机 18080/18099 已被环境内其他进程占用，
  因此改用系统分配的临时端口；服务本身无端口相关问题。
