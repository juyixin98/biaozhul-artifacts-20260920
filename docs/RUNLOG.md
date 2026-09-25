# 运行记录（RUNLOG）

本文件如实记录开发与验收过程中实际执行的命令、结果，以及出现过并已修复
的失败项。环境：Linux x86_64，Go 1.22.7（项目仅依赖标准库）。

## 1. 工具链准备

环境初始无 Go：

```text
$ go version
bash: go: command not found
```

官方源直连失败（网络受限）：

```text
$ curl -fsSL https://go.dev/dl/go1.22.7.linux-amd64.tar.gz
curl: (35) OpenSSL SSL_connect: SSL_ERROR_SYSCALL in connection to go.dev:443
```

改用国内镜像下载成功，解压到用户目录：

```text
$ curl -fsSL -o /tmp/go.tgz https://golang.google.cn/dl/go1.22.7.linux-amd64.tar.gz   # 68,981,676 字节
$ mkdir -p /home/admin/go-sdk && tar -C /home/admin/go-sdk -xzf /tmp/go.tgz
$ /home/admin/go-sdk/go/bin/go version
go version go1.22.7 linux/amd64
```

## 2. 静态检查与单元测试

```text
$ gofmt -l .
(无输出 — 全部文件已格式化)

$ go vet ./...
(无输出 — 通过)

$ go test -race -count=1 -coverprofile=/tmp/internal.cov ./internal/...
ok  	metricrollup/internal/model    	coverage: 100.0% of statements
ok  	metricrollup/internal/rollup   	coverage: 100.0% of statements
ok  	metricrollup/internal/server   	coverage: 92.6% of statements
ok  	metricrollup/internal/store    	coverage: 88.7% of statements
ok  	metricrollup/internal/synthetic	coverage: 100.0% of statements
ok  	metricrollup/internal/verify   	coverage: 89.5% of statements
$ go tool cover -func=/tmp/internal.cov | tail -1
total: (statements) 91.9%
```

`cmd/server` 与 `cmd/verify` 是薄入口，无单元测试（逻辑均在 internal 包，
已覆盖）；`-race` 在含 8 协发写入者 + 并发查询的
`TestConcurrentIngestQuery` 下无任何数据竞争报告。

全部 `go test ./...` 通过的包数：6 个 `ok`，无 `FAIL`。

## 3. 验收程序

```text
$ go run ./cmd/verify
[PASS] dense 1s regular
[PASS] dense 5s regular
[PASS] sparse 37s jittered
[PASS] sparse 90s jittered
[PASS] dense 3s with gaps
[PASS] irregular 23s jitter+gaps
[PASS] late arrivals out of order 7s
[PASS] historical corrections propagate
[PASS] empty buckets explicit
[PASS] correction after raw prune rejected
  correction after raw prune correctly rejected (3600 raw samples dropped)

10/10 scenarios passed
```

每个场景都对 raw/minute/hour 三层与"只遍历原始样本"的独立预言机逐桶比较
`count/sum/min/max`（相对容差 1e-9）。`historical corrections propagate`
的修订点覆盖 `:00`、`:59`、`02:59:59`、`03:00:00` 与桶中间位置；
`empty buckets explicit` 额外断言空桶数值字段为 `null`。

## 4. HTTP 端到端实跑

```text
$ go build -o /tmp/metricrollup-server ./cmd/server
$ /tmp/metricrollup-server -addr 127.0.0.1:18080 -data /tmp/rollup-data.json
$ BASE=http://127.0.0.1:18080 ./examples/requests.sh
exit=0
```

关键真实响应（节选）：

- 摄入：`{"ingested":7}`，两个序列（host=h1 / h2）被正确区分；
- 分钟层 02:00 桶（3 点）：`count=3 sum=35.4 min=9.8 max=13.1 mean=11.8`；
- 无数据的分钟以 `{"start":...,"count":0}` 显式返回，不跳过；
- 迟到样本 02:00:30=50.5 追加后，同一分钟桶变为
  `count=4 sum=85.9 min=9.8 max=50.5`；
- 把 02:00:00 从 12.5 修订为 42.25 后，所属小时桶自动重算为
  `count=6 sum=131.15 min=5.5 max=50.5`；
- 修订从未采集过的秒：`HTTP 409`；
- `POST /v1/admin/prune {"cutoff":10800}` →
  `{"removed_raw_samples":7}`；裁剪后修订该时段 → `HTTP 409`，但小时层
  查询仍返回修订前已落库的聚合值（有损但不丢聚合）。

快照持久化与重启恢复：

```text
$ /tmp/metricrollup-server -addr 127.0.0.1:18080 -data /tmp/rollup-data.json   # 重启进程
rollup ... snapshot loaded from /tmp/rollup-data.json
$ curl '.../v1/query?...&layer=hour&start=7200&end=10800'
{"...buckets":[{"start":7200,"count":6,"sum":131.14999999999998,...}]}   # 与重启前一致
$ curl '.../v1/query?...&layer=raw&start=7200&end=7201'
{"...buckets":[{"start":7200,"end":7201,"count":0}]}                      # 裁剪状态也被持久化
```

## 5. 开发过程中出现过的失败及修复（如实记录）

1. **int64 常量溢出（编译失败）**：合成数据生成器的哈希常量
   `0xff51afd7ed558ccd` 在 int64 运算中溢出
   （`overflows int64`）。改为用 `uint64` 运算并在转回 int64 时右移一位，
   修复。
2. **验收场景一度失败（语义 bug，已修复）**：首次运行 `cmd/verify` 为
   `9/10`，失败项 `correction after raw prune rejected`，实际得到
   `series not found`。根因：`Correct()` 对"序列存在但该秒无 raw 数据"
   与"序列不存在"都返回 `ErrNotFound`。已把前者拆分为独立错误
   `ErrRawUnavailable`（语义覆盖"从未采集"和"已裁剪"两种不可恢复情形），
   服务端映射为 409；修复后 10/10。
3. **测试编译错误**：初版确定性测试直接用 `!=` 比较含 map 的结构体
   （`invalid operation: struct containing map cannot be compared`），
   改为比较标量字段 + `reflect.DeepEqual` 比较 labels，修复。
4. **停止服务命令的坑**：`pkill -f metricrollup-server` 的 `-f` 会匹配到
   执行命令的 shell 自身，导致退出码 144；改用
   `pkill -f 'metricrollup-serv[er]'` 避开自匹配。非代码问题。

## 6. 当前未通过项 / 已知限制

- 无未通过的测试或验收场景。
- 持久化是全量 JSON 快照（非 WAL）：两次快照之间崩溃会丢失已确认摄入；
  快照写入本身是临时文件 + fsync + rename 原子替换。
- 单实例、全内存、无鉴权/限流/自动 TTL，定位为本地样例，不做生产级扩展。
- 浮点求和为朴素顺序累加，断言容差为相对 1e-9；极端量级混合输入可能需要
  Kahan/分块求和，当前合成数据范围内无误差问题。
