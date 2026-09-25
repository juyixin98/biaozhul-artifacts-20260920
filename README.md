# traceassembly — 分布式追踪 span 乱序拼装（纯后端）

用 Go 实现的可观测性数据处理后端样例：HTTP 摄入乱序 span，按 `trace_id` 拼装成
追踪树，处理缺根、重复/冲突、跨服务时钟偏差、父链循环，支持超时不完整输出与
迟到数据修订，并提供本地文件持久化与崩溃重放。全部数据为合成数据，无前端。

## 核心语义

| 能力 | 行为 |
|---|---|
| 乱序拼装 | span 按 `parent_span_id` 指针入图，**子先父后**自然容忍：父到达后边自动闭合 |
| 重复 span | 同一 `(trace_id, span_id)` 负载逐字段相同 → `exact-duplicate`，静默忽略 |
| 冲突 span | 同 id 但负载不同 → `payload-conflict`，**先到先得**，冲突字段与副本全部留痕 |
| 缺根/缺父 | 引用了不存在的父 → 记入 `missing_parents`；整树无根 → `missing-root` |
| 超时 | 逻辑水位超过 `last_touch + timeout_ns` 仍打开 → 关闭并输出 `complete=false` 的修订 |
| 迟到补全 | 已关闭 trace 收到新 span（或冲突）→ 重开，再次关闭时生成 **revision+1**，`revised_of` 指向前版 |
| 父链循环 | 沿父指针检测环（含自环），环归一化（最小 id 旋转到首位）后输出，trace 标记不完整 |
| 时钟偏差 | 仅当父子都已在图中时检查本地时间区间，产出**信息性**告警，绝不参与结构/因果判定 |
| 逐版包含 | 规范 span 集合只增不减；每个修订是不可变快照，可逐版查询并核对包含关系 |

### 关键设计决策：不用壁钟断言因果

- **因果/结构只看 `parent_span_id` 构成的图**。span 的 `start` 时间戳唯一用途是
  生成时钟偏差告警；即使“子的开始时间早于父”，子仍然是子。
- **超时只看合成逻辑时间**：摄入报文里的 `receive_ns` 与显式推进的
  `watermark_ns` 是任意整数逻辑刻度（不要求是真实纳秒）。核心包 `trace`
  **没有任何 `time.Now()` 调用**，测试无需 sleep，结果逐次确定。空闲超时由
  调用方（本样例中是测试/走查脚本）推进水位触发；把壁钟接到水位上只影响
  “何时判定空闲”，从不影响父子关系。

## 目录结构

```
trace/                  # 拼装核心（无 HTTP、无 IO 依赖，可独立使用）
  model.go              # Span / Revision / Conflict / SkewEvent 等类型
  assembler.go          # 摄入、去重冲突、水位、超时扫描、修订
  view.go               # 图分析：根、缺失父、环、偏差；查询与重放接口
store/                  # 本地持久化样例：WAL(JSONL) + 原子快照
internal/httpapi/       # net/http 路由与 JSON 编解码
cmd/trace-server/       # 服务入口
cmd/demo/               # 三幕合成场景 + 内置断言（不依赖网络/壁钟）
examples/               # 请求样例 JSON 与 curl 走查脚本
```

## HTTP API

| 方法与路径 | 说明 |
|---|---|
| `POST /v1/spans` | 批量摄入；可选 `at_least_watermark_ns` 隐式推进水位 |
| `POST /v1/watermark` | 显式推进逻辑水位 `{ "watermark_ns": 200 }` |
| `GET  /v1/traces` | 列出全部 trace 摘要与当前水位 |
| `GET  /v1/traces/{id}` | 当前视图（打开中为 `live=true` 实时视图，关闭后为冻结快照） |
| `GET  /v1/traces/{id}/revisions/{n}` | 取第 n 版不可变修订（n 从 1 开始） |
| `GET  /healthz` | 健康检查 |

Span 字段：`trace_id`、`span_id`、`parent_span_id`（根省略）、`service`、
`operation`、`start`（RFC3339）、`duration_nanos`、`attributes`、`receive_ns`（逻辑时间）。

修订 `reasons` 取值：`complete`、`timeout`、`missing-parent`、`missing-root`、
`multiple-roots`、`cycle-detected`、`late-arrival-revision`。

## 快速开始

```bash
# 构建并运行自动化测试（核心 + 持久化 + HTTP，带竞态检测）
go test -race ./...

# 三幕合成场景自检（缺根迟到、重复冲突、偏差/循环）
go run ./cmd/demo

# 启动服务（默认 :8080，数据落盘到 ./data）
go run ./cmd/trace-server -addr :8080 -data-dir ./data -timeout-ns 100 -skew-tolerance-ns 1000

# 一键 curl 走查（自动选空闲端口、自动重启验证 WAL 重放，约 30 条 jq 断言）
./examples/walkthrough.sh
```

手工调用：

```bash
curl -s -X POST localhost:8080/v1/spans -H 'Content-Type: application/json' \
  --data @examples/01-ingest-missing-root.json
curl -s -X POST localhost:8080/v1/watermark -H 'Content-Type: application/json' \
  --data @examples/02-watermark-timeout.json | jq
curl -s localhost:8080/v1/traces/tr-demo/revisions/1 | jq
```

## 持久化与重放

- `wal.jsonl`：只追加事件日志（`span` / `watermark` / `sweep`），每条 fsync。
  它是恢复的唯一事实来源；**批边界的超时扫描也记为 `sweep` 事件**，因此重放能
  在完全相同的位置重新产生相同修订。
- `snapshots/<trace_id>/rev-<n>.json`：每版修订的不可变快照，写临时文件后原子
  rename，供离线查看；恢复时以 WAL 重放为准并确定性重写快照。
- 冲突采用先到先得：规范负载永远是首份到达者，因此重放结果与历史逐字节一致
  （有 `reflect.DeepEqual` 测试覆盖跨两次重启）。

## 验收点对照

- **缺根**：`examples/01…` + 水位 200 → rev1 `complete=false`、`missing-root`/
  `missing-parent`；根在 receive_ns=300 迟到 → rev2 完整，`revised_of=1`。
- **重复 span**：`examples/05…` 同一 id 三发：接受、精确重复、负载冲突（列出
  `operation`、`duration_nanos` 两个冲突字段），规范 span 保留首份。
- **跨服务时钟偏差**：`examples/06…` 父在 svc-a 本地 10–30ms，子在 svc-b 本地
  5–40ms → 同时报 `starts-before-parent` 与 `ends-after-parent`，但 trace 结构
  仍完整、根仍由父子图唯一确定。
- **不用壁钟断言因果**：核心包无 `time.Now()`；所有断言基于逻辑水位和父指针。
- **每版包含**：测试与走查脚本均断言 rev1 的 span 集合 ⊆ rev2，且历史修订
  内容冻结、可单独查询。
- **父链循环**：`examples/07…` a→b→a 输出归一化环 `["a","b"]`，标记
  `cycle-detected`；自环在单元测试中覆盖。

## 局限（样例性质，如实说明）

- 单进程内存态 + 文件 WAL，无分片、无多副本；`sink.SaveRevision` 失败直接
  panic（宁可失败也不返回“已应答但未落盘”的假象）。
- 每条 WAL 事件 fsync，是易懂但低吞吐的样例实现。
- 时钟偏差仅做简单区间越界比较，不做时钟校正/偏移估计。
- 修订永久保留在内存与磁盘，没有保留期（TTL）与压缩。
