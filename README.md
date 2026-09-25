# histmerge — 直方图边界合并后端

纯后端 Go 项目：可观测性场景下的**累积桶直方图**摄入、合并与分位查询，
带本地 JSON 文件持久化和合成数据生成，不依赖任何真实监控平台。

## 数据模型

直方图 = 累积桶：`bounds[i]` 是第 i 个桶的上界（含），`counts[i]` 是
`value <= bounds[i]` 的观测数（累积值）。最后一个边界必须是 `+Inf`，
因此最后一个累积计数就是总观测数。

JSON 中 `+Inf` / `-Inf` 用字符串 `"+Inf"` / `"-Inf"` 表示：

```json
{"name":"svc-a.http_duration","bounds":[0.5,1,"+Inf"],"counts":[120,350,412]}
```

## 合并规则

- **边界一致** → 逐桶计数直接相加。
- **边界不一致** → 只允许向**公共粗粒度边界**收缩：取两边边界的交集
  （必含 `+Inf`），各自重分箱（rebin）到交集后再相加。累积直方图向
  自身边界的子集收缩是**无损**的（直接读取对应累积值）；绝不尝试向
  更细粒度拆分（桶内分布未知，拆分只能是编造）。
- 交集只剩 `+Inf` 时退化为单桶合并，总数仍守恒。

## 校验

摄入与合并前均校验：

- 至少一个桶，`len(counts) == len(bounds)`；
- 边界严格递增、无 NaN / `-Inf`，最后一个必须是 `+Inf`；
- 累积计数单调不减（错误累计值在此被拒绝）；
- 合并结果再次校验，并断言**计数守恒**：`total(merged) == total(a) + total(b)`。

## 分位估计

`Quantile(q)` 返回估计值与所在桶区间 `[lower, upper]`（真实分位数只能
定位到桶区间）。估计值为桶内线性插值；落入 `+Inf` 桶时无法插值，
估计值取桶下界，`upper` 为 `+Inf`。空直方图（总数为 0）查询分位返回错误。

## 构建与运行

```bash
go build -o bin/server ./cmd/server
./bin/server -addr 127.0.0.1:28081 -data data/histograms.json
```

- `-addr`：监听地址（默认 `127.0.0.1:8080`）
- `-data`：本地持久化文件（默认 `data/histograms.json`，置空则纯内存）。
  每次写入原子落盘（临时文件 + rename），重启后自动加载并重新校验。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/histograms` | 摄入一个直方图（JSON body），校验失败返回 400 |
| GET | `/v1/histograms` | 列出所有名称 |
| GET | `/v1/histograms/{name}` | 获取单个直方图 |
| POST | `/v1/merge` | body `{"names":[...]}`，按合并规则聚合，边界不兼容且无公共边界时 422 |
| GET | `/v1/quantile?name=X&q=0.95` | 分位估计（区间 + 插值），空直方图 422 |
| POST | `/v1/synth?seed=42` | 生成 3 个合成直方图（其中一个边界更粗，用于演示收缩合并） |

请求样例见 [examples/requests.sh](examples/requests.sh)：`./examples/requests.sh 127.0.0.1:28081`

## 测试

```bash
go vet ./... && go test ./...
```

覆盖验收项：无穷桶（`TestQuantileInfinityBucket` / `TestMergeDisjointBoundsCollapseToInf`）、
空直方图（`TestMergeEmptyHistogram` / `TestQuantileEmptyHistogram`）、
错误累计值（`TestValidateErrors` / `TestMergeRejectsInvalidInput` / HTTP 400 用例）、
聚合计数守恒（`TestMergeIdenticalBounds` / `TestMergeAllConservesTotal`，含运行时断言）、
分位估计区间（`TestQuantileIntervalAndEstimate`）。

## 实测记录（2026-09-24，go1.22.2 linux/amd64）

- `go vet ./... && go test ./...`：全部通过
  （`ok histmerge/internal/histogram`，`ok histmerge/internal/server`），无未通过项。
- 注意：本机 8080/18080/18099 端口被其他进程占用，实测使用 `-addr 127.0.0.1:28081`。
- `./bin/server -addr 127.0.0.1:28081 -data data/histograms.json` 启动正常。
- `POST /v1/synth?seed=42` → 生成 3 个合成直方图。
- 合并 `svc-a`+`svc-b`（同边界）→ 总数 5000+3000=8000，守恒 ✓。
- 合并 `svc-a`+`svc-b`+`svc-c`（c 边界更粗）→ 自动收缩到公共边界
  `[0.01,0.05,0.1,0.5,2.5,10,+Inf]`，总数 12000，守恒 ✓。
- `GET /v1/quantile?name=svc-a.http_duration&q=0.95`
  → `{"estimate":0.2395,"lower":0.1,"upper":0.25}`，估计值落在区间内 ✓。
- `with-inf`（counts `[8,9,10]`）q=0.95 → `{"estimate":2,"lower":2,"upper":"+Inf"}`，
  正确识别 +Inf 桶 ✓。
- 错误累计值 `counts:[9,5]` → HTTP 400 `cumulative counts must be non-decreasing` ✓。
- 缺少 `+Inf` 末边界 → HTTP 400 `last bound must be +Inf` ✓。
- 空直方图摄入成功，分位查询 → HTTP 422 `histogram is empty` ✓。
- 与空直方图合并 → 总数不变（`with-inf`+`empty` → total 10）✓。
- 杀掉进程重启后 `GET /v1/histograms` 仍返回全部 5 个直方图，持久化 ✓。

## 目录结构

```
cmd/server/main.go          入口（flag、HTTP 服务）
internal/histogram/         累积直方图：校验、合并、重分箱、分位估计（含单测）
internal/store/             JSON 文件持久化（原子写）
internal/synth/             合成数据生成
internal/server/            HTTP 接口（含集成测试）
examples/requests.sh        请求样例
```
