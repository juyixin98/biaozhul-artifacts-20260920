# HTTP API 参考

所有请求/响应均为 JSON（`Content-Type: application/json; charset=utf-8`）。
时间戳统一为**毫秒级 Unix 时间**整数。错误响应形如：

```json
{ "error": "人类可读的错误说明" }
```

## POST /v1/ingest

摄入一批样本到一条序列（按标签键控）。服务端先校验整批，再写 WAL（fsync），
最后更新内存。**任何一个样本非法都会整批拒绝**，不落盘、不改动已有数据。

请求体：

```json
{
  "labels": {"__name__": "http_requests_total", "instance": "demo-1"},
  "samples": [
    {"ts_ms": 1700000000000, "value": 0},
    {"ts_ms": 1700000010000, "value": 12}
  ]
}
```

- `labels`：非空 map，作为序列标识（键顺序无关）。
- `samples[].ts_ms`：正整数毫秒时间戳。
- `samples[].value`：计数器原始值，必须是 **≥ 0 的有限数**（负值 / NaN / Inf 拒绝）。
- 样本可以乱序；同一时间戳的同值样本按幂等重复处理，冲突值采用 max-wins。
- 请求体不允许出现未声明字段。

成功响应 `200`：

```json
{
  "status": "accepted",
  "labels": {"__name__": "http_requests_total", "instance": "demo-1"},
  "ingest": {
    "unique_samples": 10,
    "requested_samples": 5,
    "batch_duplicate_same": 1,
    "batch_duplicate_conflicts": 1,
    "overlap_same": 0,
    "overlap_conflicts": 0,
    "total_duplicate_same": 1,
    "total_duplicate_conflicts": 1
  }
}
```

非法样本响应 `400`：

```json
{
  "error": "存在非法样本，整批拒绝（未写入任何数据）",
  "rejected": [
    {"index": 1, "ts_ms": 1700000020000, "value": -1,
     "error": "计数器值不允许为负：计数器语义下原始值必须非负"}
  ]
}
```

## GET /v1/series

列出全部序列：`labels`、`sample_count`、首末时间戳、累计重复/冲突计数。

## GET /v1/increase 与 GET /v1/rate

两个端点共享同一计算结果；区别只在关注点（`increase` 与 `rate_per_second`
两个字段两端都会返回）。

查询参数：

| 参数 | 必填 | 说明 |
|---|---|---|
| `label=k=v` | 是* | 标签条件，可重复多次 |
| `labels=k=v,k2=v2` | 是* | 等价的逗号分隔写法（两种可混用） |
| `start_ms` | 是 | 窗口起点（含），毫秒 |
| `end_ms` | 是 | 窗口终点（含），毫秒，必须 > start_ms |
| `extrapolation` | 否 | `none` / `linear` / `clamped`（默认） |
| `capacity` | 否 | 正数；提供后输出条件上界，否则 `upper=null` |

\* 至少一个标签；序列不存在返回 `404`。

响应（节选，完整示例见 `docs/demo-output.txt`）：

```jsonc
{
  "labels": {"__name__": "http_requests_total", "instance": "demo-1"},
  "report": {
    "window_start_ms": 1700000000000,
    "window_end_ms":   1700000100000,
    "window_ms": 100000,
    "sample_count": 10,
    "selected_samples": [ {"ts_ms": 1700000000000, "value": 0} ],
    "median_interval_ms": 10000,
    "observed_start_ms": 1700000000000,
    "observed_end_ms":   1700000100000,
    "observed_span_ms":  100000,
    "intervals": [
      {
        "t1_ms": 1700000080000, "t2_ms": 1700000090000,
        "v1": 60, "v2": 10, "delta_ms": 10000,
        "increase_lb": 10,          // 区间严格下界（点估计）
        "increase_ub": 50,          // C=100 时的条件上界；否则 null
        "reset": true,              // 是否观测到下降
        "gap_ratio": 1, "is_gap": false
      }
    ],
    "resets": [
      {"before_ts_ms": 1700000080000, "after_ts_ms": 1700000090000,
       "before_value": 60, "after_value": 10}
    ],
    "gaps": [
      {"from_ts_ms": 1700000030000, "to_ts_ms": 1700000050000,
       "delta_ms": 20000, "median_interval_ms": 10000, "gap_ratio": 2,
       "note": "缺样区间：仅依据两个端点计算增量下界，不对区间内部行为作任何插补；区间内可能存在未观测到的重置"}
    ],
    "extrapolation": "none",
    "capacity": null,
    "raw_increase":           {"point": 75, "lower": 75, "upper": null},
    "increase":               {"point": 75, "lower": 75, "upper": null},
    "increase_denominator_ms": 100000,
    "extrapolation_factor": 1,
    "observed_coverage": 1,
    "rate_per_second":        {"point": 0.75, "lower": 0.75, "upper": null},
    "bounds_basis": "point/lower 为严格下界……未提供计数器容量……upper=null。",
    "notes": ["边界策略 none：……"]
  }
}
```

字段语义：

- `increase_lb` / `raw_increase.lower` / `increase.lower`：**严格下界**。
  重置区间按新值计；缺样只看端点。
- `*_ub` / `upper`：仅在给定 `capacity` 且“每区间至多一次重置、无隐藏回绕”
  的额外假设下成立的**条件上界**；否则为 `null`（不是 0，也不是无穷大）。
- `extrapolation_factor`：边界外推放大倍数（`none` 恒为 1）。
- `observed_coverage`：观测跨度 / 窗口跨度；显著小于 1 时 `none` 结果会偏小，
  `linear` 结果则建立在“未观测部分同速率”的强假设上。
- 空集合（无重置、无缺样）序列化为 `[]`，不是 `null`。

## GET /healthz

返回 `200 {"status":"ok"}`。

## 命令行参数（cmd/counterreset）

| 参数 | 默认 | 说明 |
|---|---|---|
| `-addr` | `:8080` | 监听地址 |
| `-data-dir` | `./data` | WAL 目录（文件名固定 `counter.wal.jsonl`） |
| `-seed` | `examples/seed.jsonl` | 合成种子 NDJSON；传空串禁用 |

退出信号（SIGINT/SIGTERM）触发优雅关闭：停止接收新连接后关闭 WAL。
