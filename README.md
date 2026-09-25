# histmerge — 直方图边界合并后端（纯后端）

用 Go 实现的可观测性数据处理样例后端：通过 HTTP 摄入**累积桶直方图**
（cumulative-bucket histogram，Prometheus 约定），跨时间序列做合并聚合，
并提供分位数估计及其**不确定区间**。数据为进程内合成数据，附带本地
JSONL WAL 持久化。无真实监控平台依赖、无前端。

## 核心语义（本项目最重要的约定）

1. **桶是累积的**：每个桶 `{upper, cumulative_count}` 表示 `观测值 <= upper`
   的总数；最后一个桶上界必须是 `"+Inf"`，其计数必须等于 `total_count`。
2. **只有边界布局完全相同才能直接合并**（`MergeStrict`）：逐桶计数相加。
3. **布局不兼容时，只允许向“公共粗粒度边界”收缩**（`MergeCoarsen`）：
   公共边界 = 所有输入有限上界的**交集** ∪ `+Inf`。每个直方图只能保留
   自己本来就有的边界（子集/contract），绝不凭空插值出新边界的精确计数；
   被丢弃的细粒度信息是有意为之。若连一个公共有限边界都没有，返回
   `ErrNoCommonBounds`，拒绝合并。
4. **校验**：
   - 累积计数单调不减；
   - `+Inf` 桶计数 == `total_count`（计数守恒的结构约束）；
   - 空直方图（`total_count=0`）所有计数必须为 0；
   - 摄入同一序列时，后续样本必须时间戳递增、`total_count` 与各共享桶计数
     不减少（检测 counter reset / 错误累计值）、`sum` 不回落。
5. **合并守恒检查**：结果里同时给出
   `total_count_conserved`（合并总数 == 各输入总数之和）与
   `bucket_count_conserved`（收缩后每个保留桶 == 各输入在该边界计数之和）。
6. **分位估计区间**：点估计采用桶内线性插值（Prometheus 风格）；区间是
   该秩所在桶的上下边。落在开放 `+Inf` 桶时不给伪造点估计（`point=null`），
   区间上边开放（`upper=null` 表示 +Inf）；空直方图拒绝求分位。

## 目录结构

```
cmd/histmerge/            服务入口 + seed 子命令（合成数据播种）
internal/histogram/       领域核心：边界、校验、收缩、合并、分位区间
internal/store/           内存序列存储 + JSONL WAL、单调性/乱序校验、窗口增量
internal/server/          HTTP 路由与处理
internal/synthetic/       确定性合成数据（刻意使用两种不同桶布局）
examples/                 请求样例 JSON + demo.sh
data/                     默认 WAL 目录（运行时生成）
```

## 构建与运行

要求 Go 1.22+。

```bash
go build ./...
go test -race -cover ./...

# 启动服务（默认 :8080，WAL 在 data/histmerge.wal.jsonl）
go run ./cmd/histmerge -addr :8080 -wal data/histmerge.wal.jsonl

# 另一个终端：播种合成数据（3 个有流量序列 ×6 次累积快照 + 1 个空直方图）
go run ./cmd/histmerge seed -addr http://127.0.0.1:8080 -scrapes 6 -seed 42

# 或一键端到端演示（自动起临时服务、发各种请求、退出）
./examples/demo.sh 18080
```

停掉再启动同一 WAL 路径，历史样本会自动重放。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/healthz` | 健康检查 |
| POST | `/api/v1/ingest` | 摄入单样本 `{...}` 或批量 `{"samples":[...]}` |
| GET  | `/api/v1/series` | 列出序列（支持 `?match[]=name=value`） |
| GET/POST | `/api/v1/query` | 合并各序列**最新**样本并可选求分位 |
| GET/POST | `/api/v1/query_range` | 窗口内每个序列先求增量（increase），再合并 |

查询参数（GET）：`match[]=name=value`（可重复）、`quantile=0.5`（可重复）、
`coarsen=true|false`（默认 true；设为 false 时布局不兼容返回 409）、
`from/to`（RFC3339，仅 range）。POST 用 JSON body，字段同名（`match`、
`quantiles`、`coarsen`、`from`、`to`）。

### 摄入载荷示例

```json
{
  "labels": [{"name": "service", "value": "checkout"}],
  "histogram": {
    "total_count": 10,
    "sum": 2.34,
    "buckets": [
      {"upper": 0.1,   "cumulative_count": 4},
      {"upper": 1,     "cumulative_count": 9},
      {"upper": "+Inf", "cumulative_count": 10}
    ]
  }
}
```

`+Inf` 在线路上写成字符串 `"+Inf"`；有限上界是数字。时间戳可省略
（取服务端当前时间）。

### 典型 curl

```bash
curl -s -X POST localhost:8080/api/v1/ingest \
  -H 'Content-Type: application/json' --data-binary @examples/ingest_compatible.json

curl -s 'localhost:8080/api/v1/query?match[]=service=checkout&quantile=0.5&quantile=0.99'

# 严格模式：布局不兼容直接 409，不做收缩
curl -s 'localhost:8080/api/v1/query?coarsen=false'
```

### 响应中的关键字段

```json
{
  "series_count": 2,
  "strategy": "coarsened",          // direct | coarsened
  "common_bounds": ["0.1","10","+Inf"],
  "merge": { "total_count": 17, "sum": 13.54, "buckets": [ ... ] },
  "input_total_count": 17,
  "total_count_conserved": true,
  "bucket_count_conserved": true,
  "quantiles": [
    {"q":0.5,"rank":8.5,"point":2.575,"lower":0.1,"upper":10,"finite_interval":true},
    {"q":0.99,"rank":16.83,"point":null,"lower":10,"upper":null,
     "finite_interval":false,"note":"rank falls inside the open +Inf bucket"}
  ],
  "empty": false
}
```

## 错误处理（HTTP 状态码）

- `422`：直方图结构非法（非单调、缺 +Inf、+Inf 计数不等于总数、空直方图
  带非零计数、分位越界、无公共边界等）；
- `409`：严格模式下布局不兼容；或摄入出现 counter reset / 时间戳乱序；
- `404`：选择器/窗口内没有可聚合序列；
- `400`：JSON 或选择器格式错误。

摄入是逐条返回结果的批量接口：部分成功时 `200`（全冲突 `409`，
全结构非法 `422`），每条带 `ok/error`。

## 持久化

WAL 为 append-only JSONL（每行一条 `{"kind":"sample",...}`），摄入校验通过
后每条立即 flush。启动时重放重建内存态；坏行会跳过并在启动错误信息中如实
报告跳过条数。这是样例级持久化，不做压缩/分段。

## 测试

```bash
go test ./...                  # 全部单元 + HTTP 端到端测试
go test -race -cover ./...     # 竞态检测 + 覆盖率
```

覆盖的验收点：无穷桶（全在 +Inf、秩落入 +Inf、q=1 下边）、空直方图
（加法单位元、全空合并、拒绝求分位）、错误累计值（结构非单调、
+Inf/total 不符、摄入期 counter reset 与乱序）、聚合计数守恒
（直接合并与收缩合并两层）、分位点落在区间内（含开放区间无点估计）、
WAL 重放、窗口增量。

已知边界（样例性质，未做）：分布式分片、WAL 压缩/快照、认证鉴权、
基数/内存上限治理、直方图浮点和的 Kahan 补偿。
