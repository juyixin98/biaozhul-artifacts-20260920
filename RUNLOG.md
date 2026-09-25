# 运行记录（RUNLOG）

环境：Linux 6.8.0-90-generic x86_64，Go 1.22.2。日期 2026-09-24。
以下命令均在项目根目录实际执行，输出为真实结果（仅在长 JSON 处节选）。

## 1. 编译与静态检查

```
$ go build ./...
$ go vet ./...
$ gofmt -l .
（首次有 4 个文件未对齐：merge.go quantile.go server.go store.go；
 已执行 gofmt -w，之后 gofmt -l . 无输出）
```

## 2. 自动化测试

```
$ go test -race -cover ./... -count=1
ok  	histmerge/internal/histogram	1.027s	coverage: 83.9%
ok  	histmerge/internal/server	1.046s	coverage: 68.2%
ok  	histmerge/internal/store	1.020s	coverage: 79.2%
ok  	histmerge/internal/synthetic	1.028s	coverage: 97.6%
?   	histmerge/cmd/histmerge	[no test files]
```

- `-race` 无数据竞争报告。
- 测试用例数：`go test -v` 中 `--- PASS` 48 个，`--- FAIL` 0 个。
- 开发过程中真实失败并修正的用例（已修复，现全部通过）：
  1. `TestMergeCoarsen_CountConservation`：测试期望被我两次手算错
     （公共边界 4 处 a=6、b=5，合计应为 11），改正期望值后通过；
     实现本身输出 11 是正确的。
  2. `TestIngest_CounterResetAcrossRequests`：整批被拒时服务最初一律
     返回 422，与“counter reset 属冲突 409”的语义不符；已在 ingest
     处理中按错误类型区分 409/422，之后通过。

### 测试覆盖的验收点

- 无穷桶：全部观测落入 +Inf（区间 (10,+Inf)、无点估计）；仅有 +Inf
  桶的布局（区间 (-Inf,+Inf)）；秩落入 +Inf（q=0.9）；q=1 取 +Inf 桶
  下边。
- 空直方图：合法空直方图校验通过；作为加法单位元参与合并；全空合并
  结果 total=0；对空直方图求分位返回 `ErrEmptyHistogram`；空直方图
  带非零计数被判非法。
- 错误累计值：载荷内累积计数下降、缺 +Inf 桶、重复边界、+Inf 计数≠
  total；摄入时 total 回退、单桶计数回退（total 仍增长）、时间戳乱序。
- 聚合守恒：直接合并与收缩合并都校验 total 与桶级守恒标志；合成数据
  属性测试跨两种布局随机生成后守恒成立。
- 分位区间：点估计落在 [lower, upper] 内；开放桶不产出点估计；q=0
  下边为 -Inf；非法 q（<0、>1、NaN）报错。
- 持久化：写入 WAL → 关闭 → 用同一路径重新打开 → 序列与计数完整恢复。
- 窗口增量：increase 差分桶计数且差分结果自身仍是合法累积直方图。

## 3. 实际启动与端到端（合成数据）

构建二进制：

```
$ go build -o bin/histmerge ./cmd/histmerge
```

端口说明：首次尝试 :18080 时发现已被本机无关进程 `vccsim` 占用，
:18091 被无关进程 `ws` 占用（曾误把它的应答当成我们的服务，后用
`ss -tlnp` 确认进程名排除）。最终由内核分配空闲端口 55663，并确认
监听者为 `histmerge`：

```
$ ./bin/histmerge -addr :55663 -wal /tmp/hm-data/wal.jsonl
2026/... histmerge listening on :55663 (wal="/tmp/hm-data/wal.jsonl")
$ ss -tlnp | grep 55663
LISTEN *:55663 users:(("histmerge",pid=2058622,fd=7))
$ curl -s http://127.0.0.1:55663/healthz
{"status":"ok"}
```

播种（3 个有流量序列各 6 次累积快照 + 1 个空直方图 = 19 样本）：

```
$ ./bin/histmerge seed -addr http://127.0.0.1:55663 -scrapes 6 -seed 42
seeded 19 samples; ingest status 200 OK
{"accepted":19,"rejected":0, ...}
```

即时聚合（两种布局自动收缩到交集 0.01/0.1/0.5/2.5/10/+Inf）：

```
$ curl -s '.../api/v1/query?quantile=0.5&quantile=0.95&quantile=0.99'
"strategy":"coarsened",
"merge":{ "total_count":962, ... buckets ... "+Inf":962 },
"input_total_count":962,
"total_count_conserved":true, "bucket_count_conserved":true,
quantiles: p50=0.18897 ∈ [0.1,0.5]; p95=0.49708 ∈ [0.1,0.5];
p99 point=null, interval [10,+Inf), note="rank falls inside the open +Inf bucket"
```

严格模式拒绝不兼容布局：

```
$ curl '.../api/v1/query?coarsen=false'
HTTP 409
{"error":"incompatible bucket layout: [0.03 0.1 ... 30 +Inf] vs [0.005 0.01 ... 10 +Inf]"}
```

错误累计值（摄入一个 total_count 回退的样本）：

```
HTTP 409
{"accepted":0,"rejected":1,"results":[{"ok":false,
 "error":"counter reset: sample is not monotonic: total_count 1 < previous 308"}]}
```

结构非法（+Inf 计数≠total；载荷内非单调）：均 `HTTP 422`，错误信息分别为
`+Inf bucket count 4 != total_count 5`、
`cumulative counts decrease: 1=3 > 2=2`。

范围查询（窗口内先求每序列增量再合并；空服务仅 1 个样本被跳过并注明）：

```
"series_count":3, "strategy":"coarsened",
"merge":{ "total_count":822, ... "+Inf":822 },
"input_total_count":822, "total_count_conserved":true,
"note":"1 series skipped (counter reset or <2 samples in window)"
```

WAL 持久化实测：杀掉服务后磁盘上有 19 行 JSONL；用同一 WAL 重启，
`/api/v1/series` 恢复出 4 个序列（6/6/6/1 个样本），聚合总数仍为 962。

## 4. 一键演示

```
$ examples/demo.sh 32879
```

实际运行通过：健康检查、兼容/不兼容/空直方图摄入、两种 422 非法载荷、
严格模式 409、自动收缩聚合（p50 点估计在区间内、p95/p99 落入开放 +Inf
不给点估计）、GET 选择器查询、序列列表，共 11 个步骤全部符合预期。

## 5. 未通过项 / 已知限制

- 无未通过的测试或验收项（48/48 通过，race 干净）。
- 样例级限制（刻意未做）：无鉴权、无 WAL 压缩/快照、无基数治理、
  非分布式、sum 为朴素浮点累加。WAL 若含损坏行会跳过并在启动报错信息中
  报告跳过条数（不阻断其余数据恢复）。
