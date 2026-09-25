# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2，纯标准库（无外部依赖）。
以下命令均在项目根目录实际执行，输出为真实结果。除开发过程中修复的两次
测试预期/实现偏差外（见第 7 节，如实记录），最终全部通过，无未通过项。

## 1. 构建

```
$ go build -o cpathtrace ./cmd/cpathtrace
BUILD_OK
```

`go vet ./...`：无输出（干净）。`gofmt -l .`：最终无输出（干净；过程中
server.go 有一处字段对齐问题，已 `gofmt -w` 修正）。

## 2. 单元/集成测试

```
$ go test ./...
ok  	cpathtrace/internal/analyzer
ok  	cpathtrace/internal/api
ok  	cpathtrace/internal/store
?   	cpathtrace/cmd/cpathtrace	[no test files]
?   	cpathtrace/internal/model	[no test files]
?   	cpathtrace/internal/synthetic	[no test files]
```

逐用例（`go test -v ./...`，共 18 个，全部 PASS）：

- analyzer：TestSerialParallelHandCalculation、TestOverlappingIntervalsUnionSelfTime、
  TestSelfTimeNeverSumsChildren、TestSelfTimeClipping、TestMissingSpan、TestCycle、
  TestPureCycleNoRoot、TestNegativeDurationAndClockContradictions、
  TestMultipleRootsPicksEarliest、TestAsyncMaxNotSum、TestSyncSerialSum（11 个）
- api：TestIngestAndAnalyze、TestCycleReturns422、TestSeedScenarios、
  TestValidationFailures、TestHealthz（5 个）
- store：TestSaveGetListDelete、TestRejectsPathTraversal（2 个）

竞态检测：

```
$ go test -race ./...
ok  cpathtrace/internal/analyzer   1.015s
ok  cpathtrace/internal/api        1.047s
ok  cpathtrace/internal/store      1.021s
```

覆盖率：

```
$ go test -coverprofile=/tmp/cpathtrace.cover ./...
ok  cpathtrace/internal/analyzer   coverage: 95.7%
ok  cpathtrace/internal/api        coverage: 75.2%
ok  cpathtrace/internal/store      coverage: 70.3%
total: (statements) 74.2%
```

（cmd/main、model 的 Normalize、synthetic 构造器未被覆盖统计单独计入，
属入口/样板代码。）

## 3. CLI：离线分析

```
$ ./cpathtrace analyze --file examples/requests/01-ingest-serial-parallel.json
```

关键结果（完整 JSON 见命令输出）：

```
trace_duration: 120
critical_path_duration: 140
critical_to_wall_ratio: 1.1667
critical_path: root -> s1 -> a1 -> x -> y -> s2
self_time: root=0 s1=20 a1=0 x=50 y=50 a2=30 s2=20
diagnostics: CRITICAL_EXCEEDS_DURATION (warning, span root)
```

与 README 第 3.3 节手算一致：`C(root)=0+(20+20)+max(100,30)=140`。

## 4. CLI：合成数据落盘

```
$ ./cpathtrace seed --name all --data /tmp/cpdata
seeded serial_parallel as trace demo-serial-parallel (7 spans)
seeded sync_overlap    as trace demo-sync-overlap    (4 spans)
seeded missing_span    as trace demo-missing-span    (3 spans)
seeded cycle           as trace demo-cycle           (4 spans)
seeded clock_skew      as trace demo-clock-skew      (3 spans)
```

`data/<trace_id>.json` 文件持久化成功（原子 rename 写入）。

## 5. HTTP 端到端（`./examples/curl-demo.sh`，退出码 0）

`./cpathtrace serve --addr :8080 --data ./data` 后运行脚本，各场景实测：

| 场景 | 实测关键路径 | 诊断 / HTTP |
| --- | --- | --- |
| serial_parallel | **140** | CRITICAL_EXCEEDS_DURATION(warning)，200 |
| sync_overlap | **200**（>墙钟100） | SYNC_CHILD_OVERLAP + CRITICAL_EXCEEDS_DURATION，200 |
| missing_span | **100** | MISSING_PARENT(span=o)，孤儿 o 不在路径中，200 |
| cycle | `null`，路径 `[]` | **HTTP 422**，`CYCLE` error：`a -> b -> c -> a` |
| clock_skew | **160** | 2 条 CHILD_OUTSIDE_PARENT + CRITICAL_EXCEEDS_DURATION，200 |

clock_skew 手算核对：self(root)=100−(裁剪后 [0,30)∪[60,100))=30；
C(root)=30+50(sync c1)+80(async c2 的 max)=160。✓

错误处理实测：

```
$ curl -X POST .../api/traces -d '{"trace_id":"x","spans":[{"span_id":"a","name":"a","bogus":1}]}'
{"error":{"code":"invalid_json","message":"json: unknown field \"bogus\""}}     HTTP 400

$ curl .../api/traces/nope/critical
{"error":{"code":"not_found","message":"trace nope not found"}}                 HTTP 404
```

合成播种 `name=nope` 返回 400；GET `/healthz` 返回 `{"status":"ok"}`；
GET `/api/traces` 返回 10 个 trace_id（5 个请求样例 + 5 个合成）。

## 6. 验收点对照

- [x] 可手算串并行 DAG 验证路径：140 手算 = 实算；路径含胜出 async 分支、
      排除输家 a2（TestSerialParallelHandCalculation 逐条断言 self/C/路径）。
- [x] 区分同步依赖与并行子任务：sync 求和、async 取 max（TestAsyncMaxNotSum、
      TestSyncSerialSum）。
- [x] 不直接求所有子耗时之和：自耗时用区间并集（TestSelfTimeNeverSumsChildren、
      TestOverlappingIntervalsUnionSelfTime）。
- [x] 覆盖重叠区间：sync 重叠告警 + 并集去重（sync_overlap 场景）。
- [x] 缺 span：MISSING_PARENT 诊断，孤儿隔离（missing_span 场景）。
- [x] 循环输入：CYCLE error + 422 + 路径 null（cycle 场景，含根不可达环与纯环）。
- [x] 时钟矛盾诊断：负/零时长、子区间越界、关键时间超墙钟（clock_skew 等）。
- [x] 解释自耗时规则：README 第 3.1 节（裁剪 → 排序合并 → 时长减并集）。
- [x] 源码、README、请求样例、自动化测试；实际运行记录见本文件；无前端。

## 7. 开发过程中出现并已修复的问题（如实记录）

1. 关键路径排序的最初实现按“纯墙钟开始时间”对所有路径节点重排，导致同一时刻
   启动的父子节点（a1 与 x 都在 t=20）被拆散，路径树不可读。修正为 DFS 树顺序
   （sync 子树与胜出 async 子树按启动时间交错、每个分支链保持连续），并同步更新
   测试预期为 `root -> s1 -> a1 -> x -> y -> s2`。
2. 初版 TestAsyncMaxNotSum 的样例区间把父节点完全覆盖（self=0），期望值 110
   与公式不符；调整子区间为 [10,40)/[10,100) 使 self=10、关键时间=
   `10+max(30,90)=100`，测试名与断言一致。
3. 两处编译期笔误（临时辅助函数名）在 `go build`/`go vet` 阶段即被发现并修复，
   未进入测试运行。

最终状态：构建成功，18/18 测试通过（含 -race），HTTP 五类场景实测符合手算，
**无未通过项**。
