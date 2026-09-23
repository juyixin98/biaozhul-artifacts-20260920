# 时序计数器重置 · 纯后端样例（Go）

一个**可观测性数据处理后端**的教学/样例实现：通过 HTTP 摄入单调计数器（monotonic
counter）样本，持久化到本地，并对任意查询时间窗计算**区间增长量（increase）**与
**速率（rate）**，正确处理：

- **计数器重置（counter reset）**：值下降时按 Prometheus 约定识别并修正；
- **缺样（missing samples）**：相邻采样间隔异常大时标记为未观察区间，不计入增长；
- **重复时间戳**：相同值幂等忽略，不同值返回 `409 Conflict`；
- **乱序（out-of-order）**：摄入顺序无关，分析前按时间归一化排序；
- **查询边界切割**：窗口起止落在两个样本之间时做**线性插值**，并给出诚实的
  `[min, max]` 估计范围；
- **绝不外推**：对首个样本之前、末个样本之后以及缺样区间的内部，不做任何无依据的
  精确外推，只报告“未观察”的时长。

无前端。所有数据均为**合成数据**（进程启动时自动 seed，或用 API 手动写入），不依赖
任何真实监控平台。

## 目录结构

```
.
├── go.mod
├── main.go              # HTTP 服务入口（信号优雅退出、启动 seed）
├── counter/             # 纯领域核心：区间增长/速率/重置/插值（无 I/O）
│   ├── counter.go
│   └── counter_test.go  # 手算验收序列 + 边界/重置/缺样测试
├── store/               # 内存序列存储 + JSONL WAL 持久化与重放
│   ├── store.go
│   └── store_test.go
├── api/                 # HTTP handler（摄入/查询/列表/重置）
│   ├── api.go
│   ├── fluxtime.go      # Unix 秒 或 RFC3339 时间解析
│   └── api_test.go
├── seed/                # 确定性合成数据
└── examples/            # 请求样例与端到端脚本
    ├── ingest_main.json
    ├── ingest_out_of_order.json
    └── demo.sh
```

## 运行

需要 Go 1.22+。

```bash
go run . -addr :8080 -data ./data
# 可选参数：
#   -addr string  监听地址（默认 ":8080"）
#   -data string  WAL 目录（默认 "data"），删除该目录即清空全部数据
#   -seed         存储为空时自动载入合成数据（默认 true）
```

启动后健康检查：

```bash
curl -s localhost:8080/healthz
# {"status":"ok"}
```

一键端到端演示（自动起服务、发请求、打印结果、退出清理）：

```bash
bash examples/demo.sh
```

## HTTP API

时间 `t` / 查询参数 `from`、`to` 均支持 **Unix 秒（数字，可带小数）** 或
**RFC3339 字符串**（如 `2023-11-14T22:13:20Z`）。

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET`  | `/healthz` | 健康检查 |
| `POST` | `/api/v1/series` | 摄入一个序列的一批样本 |
| `GET`  | `/api/v1/series?metric=` | 列出序列（可按 metric 过滤） |
| `GET`  | `/api/v1/query?metric=&from=&to=` | 窗口增长/速率查询 |
| `POST` | `/api/v1/admin/reset` | 清空全部数据（记一条 WAL 标记） |

### 摄入

```bash
curl -s -X POST localhost:8080/api/v1/series \
  -H 'Content-Type: application/json' \
  -d @examples/ingest_main.json
```

请求体：

```json
{
  "metric": "requests_total",
  "labels": {"service": "demo", "host": "synthetic-1"},
  "samples": [
    {"t": 0, "value": 10},
    {"t": 60, "value": 20}
  ]
}
```

序列身份 = metric + 全部 label（label 顺序无关）。校验为**整批原子**：任一样本非法则
整批拒绝，不写内存也不写 WAL。

- `value < 0`、`NaN`、`±Inf` → `400`（计数器单调非负，负值拒绝）；
- 同一 `t` 出现两次且**值相同** → 幂等忽略，响应里计 `duplicate_ignored`；
- 同一 `t` **值不同** → `409 Conflict`（无法判断哪个为真，拒绝而不是猜测）；
- 乱序样本直接接收，存储层始终保持时间有序。

### 查询

```bash
curl -s "localhost:8080/api/v1/query?metric=requests_total&from=0&to=600" | jq
```

可选参数：

- `label.<name>=<value>`：标签选择器（需全部包含且相等，允许序列有额外 label）；
- `max_interval=<秒>`：缺样判定阈值。相邻样本间隔 **严格大于** 该值的区间视为缺样。
  省略时自动取 **1.5 × 采样间隔中位数**（本序列中位数 60s → 阈值 90s，故 180→300 的
  120s 间隔被判为缺样）。

响应核心字段：

```json
{
  "increase": 75,            // 点估计
  "min_increase": 75,        // 诚实下界
  "max_increase": 75,        // 诚实上界
  "resets": [{"at": 540, "from_value": 100, "to_value": 5, "increase": 5}],
  "segments": [ ... ],       // 每个相邻区间：observed / interpolated / gap
  "window_duration": 600,
  "observed_duration": 480,
  "gaps_duration": 120,
  "unobserved_before": 0,
  "unobserved_after": 0,
  "coverage": 0.8,
  "has_data": true,
  "window_rate":   {"per_second": 0.125, "min_per_second": 0.125, "max_per_second": 0.125},
  "observed_rate": {"per_second": 0.15625, ...},
  "missing_samples": 1,
  "notes": ["missing_samples:180->300", "resets_detected:1"]
}
```

两个速率口径：`window_rate = 增长量 / 窗口时长`（缺口也算时间，保守），
`observed_rate = 增长量 / 实际观察时长`（缺口排除）。窗口内无有效数据时速率为 `null`，
不输出 0 来假装观察过。

## 计算规则与语义

设相邻两个已排序样本为 `a=(t₁,v₁)`、`b=(t₂,v₂)`，`Δt = t₂−t₁ > 0`，`Δv = v₂−v₁`。

1. **正常区间（Δv ≥ 0）**：区间增长 = `Δv`。
2. **重置区间（Δv < 0）**：计数器被归零/重启。采用与 Prometheus `rate`/`increase`
   一致的修正（已对照 `promql/functions.go` 的 `extrapolatedRate` 核实）：

   ```
   总增长 = (末值 − 首值) + Σ 每次重置时重置前最后一个观测值
   ```

   对单个相邻对而言，重置对的贡献 = **重置后的新值 `v₂`**。直观含义：`v₂−v₁` 为负，
   补回一个完整的旧计数器寿命 `v₁`，得 `v₂−v₁+v₁ = v₂`；“重置前最后一个观测点到
   真正归零那一刻”旧计数器还涨了多少无从得知，保守计 0（可能低估，绝不高估）。
3. **缺样区间**：`Δt > max_interval` 时，内部没有任何观测，增长贡献 **0**，单独标记
   `kind="gap"`，并计入 `gaps_duration` / `missing_samples`。不用两侧斜率去填平。
4. **窗口边界线性插值**：当查询窗口在某个区间**内部**切开时，只对被切到的那一段按时长
   比例线性分摊该区间的增长：`段增长 = Δv × (段时长 / Δt)`。这是唯一的插值，且只发生
   在边界，最多两段。
5. **边界切到重置区间**：线性插值要求区间内匀速，但重置时刻未知（可能在切段内任意
   位置），因此**不做线性外推**：切段点估计取 0，并给出范围 `[0, 该重置对增长]`，同时
   置 `interpolated_across_reset`。
6. **不外推（no extrapolation）**：首样本之前与末样本之后的时间计入
   `unobserved_before/after`，增长恒为 0；`coverage` 明确告诉你结论覆盖了窗口的多大
   比例。这与 Prometheus 的做法**刻意不同**——Prometheus 会把速率向窗口两端外推，本
   项目按题目要求拒绝对未观察区间作无依据的精确外推。

### 估计范围 `[min_increase, max_increase]` 的含义

- 完整落在窗口内的观察区间：`min = max = 点估计`（该段增长被两端样本夹住，确定）。
- 线性插值的边界段：真实增长只能落在 `[0, 整个区间增长]`，点估计取线性中值。因此
  `min/max` 会比点估计更宽，宽度等于被切区间的整段增长。
- 切到重置区间的边界段：`[0, 重置对贡献]`。
- 缺样段：不贡献任何数值，但会通过 `gaps_duration`、`coverage`、`notes` 显式暴露
  不确定性，而不是把它悄悄算成 0 增长。

**速率范围同理**：`min/max_per_second` 用相同的 `min/max` 增长量除以相同的分母。

## 手算验收（含多次重置）

### 序列一：缺样 + 一次重置（`seed.MainSeries`，测试 `TestHandComputedFullWindow`）

```
 t:    0   60  120  180 │300  360  420  480  540  600
 v:   10   20   30   40 │ 70   80   90  100    5   15
                    t=240 缺样（180→300 间隔 120s > 阈值 90s）
                                        540: 100→5 重置
```

逐对（窗口 `[0,600]`，每段 60s）：

| 区间 | Δv | 判定 | 计入增长 |
|---|---|---|---|
| 0→60、60→120、120→180 | 各 +10 | 正常（gap 之前 3 对） | 30 |
| 180→300 | +30 | **缺样**，间隔 120s > 阈值 90s，内部未观察 | 0（标记 gap，120s） |
| 300→360、360→420、420→480 | 各 +10 | 正常（gap 之后 3 对） | 30 |
| 480→540 | −95 | **重置**，贡献 = 新值 5 | 5 |
| 540→600 | +10 | 正常 | 10 |

合计：`increase = 30 + 0 + 30 + 5 + 10 = 75`，与测试断言一致。

- `observed_duration = 600 − 120(gap) = 480s`，`coverage = 0.8`
- `window_rate = 75 / 600 = 0.125 /s`
- `observed_rate = 75 / 480 = 0.15625 /s`
- 无边界切割，故 `min = max = 75`。

### 序列二：两次重置（测试 `TestHandComputedMultipleResets`）

```
 t:  0    10   20   30   40   50
 v: 100  200   5   10    2    8
          ↑重置     ↑重置
```

- 0→10：`+100`
- 10→20：重置，贡献新值 `5`
- 20→30：`+5`
- 30→40：重置，贡献新值 `2`
- 40→50：`+6`

合计 `increase = 100+5+5+2+6 = 118`。用总公式复核：
`(8 − 100) + 200 + 10 = −92 + 210 = 118` ✓

### 边界插值（测试 `TestBoundaryInterpolation`，窗口 `[30,570]`）

- 内部完整对：五个 +10 加重置对 5 = `55`（确定部分，min 也是 55）；
- 起点切在 0→60 中间（30/60=1/2）：线性估计 `10×0.5 = 5`，范围 `[0,10]`；
- 终点切在 540→600 中间（30/60=1/2）：线性估计 `10×0.5 = 5`，范围 `[0,10]`；
- gap 180→300 仍排除。

结果：**点估计 65，`min=55`，`max=75`**，`observed_duration=420`，`coverage=420/540`。

### 边界切到重置（测试 `TestBoundaryAcrossReset`，窗口 `[0,510]`）

终点 510 落在重置对 480(100)→540(5) 内：重置时刻未知，30s 切段**不线性外推**，
点估计 0、范围 `[0,5]`。其余为 gap 前三个 +10 与 gap 后两个 +10：
**点估计 60，`min=60`，`max=65`**。

### 负值拒绝与边界策略小结

- 摄入期 `value<0` 一律 `400`（见 `TestValidateSample/negative_value_rejected` 与
  `TestNegativeValueHTTPRejected`），不存在“把负值当重置”的情况——重置只能由相邻样本
  的下降在分析期识别；
- 边界只做**线性插值**，不做常量外推、不做趋势外推；
- 边界遇上重置则放弃插值、给范围；缺口与首尾未观察区只报告时长，不猜数值。

## 持久化

- 每批成功摄入追加一行 JSON 到 `data/wal.log` 并 `fsync`，随后更新内存；
- 启动时重放整个 WAL 重建状态；`POST /api/v1/admin/reset` 写一条 `{"op":"reset"}`
  标记，重放时清空（见 `TestWALReplay`、`TestReset`）；
- 这是样例级实现：单文件 WAL、无压缩/分段、全局互斥锁，不追求写入吞吐。

## 测试

```bash
go test ./...           # 全部
go test -v ./counter/   # 手算序列逐项核对
go test -race ./...     # 竞态检测
go vet ./...
```

测试覆盖：多次重住手算、单次重置 + 缺样手算、两端边界插值、边界跨重置、首尾不外推、
空窗口、乱序归一化、显式/自动缺样阈值、负值/NaN/Inf 拒绝、常量计数器、WAL 重放、
重复时间戳（幂等与 409）、label 身份、HTTP 端到端与 RFC3339 时间。

## 明确不做的事

- 不对外推未观察区间（首前、尾后、缺口内部）给精确增长/速率；
- 不在边界对重置区间做线性插值；
- 不猜测冲突时间戳的真值；
- 不做前端、不接真实监控系统、不实现 PromQL。
