# logcluster — 日志模板在线聚类后端

纯后端 Go 项目：通过 HTTP 摄入原始日志行，在线聚类为有界数量的日志模板，
支持查询、容量淘汰与本地快照持久化。数据为合成数据，不依赖真实监控平台。

## 设计要点

### 分词与变量掩码（`internal/cluster/tokenize.go`）

每行按空白分词后，逐 token 按优先级掩码：

| 规则 | 示例 | 掩码结果 |
|---|---|---|
| UUID | `550e8400-...-446655440000` | `<UUID>` |
| IPv4（可带端口） | `10.0.0.1:8080` | `<IP>` |
| 数字（含单位/百分号/千分位） | `42`, `120ms`, `99%` | `<NUM>` |
| 十六进制（≥8 位且含数字，或 `0x` 前缀） | `0xdeadbeef` | `<HEX>` |
| 路径中的纯数字段 | `/api/v1/orders/1000` | `/api/v1/orders/<NUM>`（`v1` 等关键字保留） |
| `key=value` | `status=200` | `status=<VAL>`（键保留） |
| 词缀数字 | `worker-7` | `worker-<NUM>` |
| 引号串 | `"some text` | `<STR>` |

注意顺序：数字先于十六进制判断，避免 8 位以上十进制数被误判为 `<HEX>`。

### 合并规则与防过度泛化（`internal/cluster/template.go`）

- 候选桶：`(token 数, 首 token)`，只在同桶内匹配，保证效率与确定性。
- 逐位置匹配：相同 token 或既有通配符 `*` 直接匹配；**两种不同变量类型**、
  或**模板为字面量而日志为变量**时，该位置泛化为 `*` 并 bump 模板版本；
  **两个不同字面量（关键字）永不合并** —— `read`/`write`、`timeout`/`refused`
  这类差异永远留在不同模板中，这是避免"过度泛化合并不同错误"的核心保证。
- 通配符比例守卫：单次泛化若使 `*` 占比超过 `-max-wild-ratio`（默认 0.4），
  拒绝合并，另建新模板。
- 每次泛化产生一条版本记录（`history`），模板版本从 v1 递增。

### 有界容量与淘汰（`internal/cluster/cluster.go`）

模板数超过 `-capacity` 时淘汰 `LastSeq` 最小（最久未命中）的模板，并列时取
ID 最小者，淘汰事件记入日志，完全确定性。被淘汰模板再次出现时以新 ID 重建。

### 超长行处理

行超过 `-max-line-bytes`（默认 64KiB）按 rune 边界截断；token 数超过
`-max-tokens`（默认 256）截断；两种情况都追加 `<TRUNC>` 标记。

### 持久化（`internal/cluster/persist.go`）

`-data <file>` 启用：收到 SIGTERM/SIGINT 或调用 `POST /v1/snapshot` 时，
将全量模板、序列号、淘汰日志以 JSON 原子写入（临时文件 + rename）；
启动时若文件存在则恢复，聚类状态无缝延续。

## 目录结构

```
cmd/logcluster/   HTTP 服务入口
cmd/eval/         确定性评估入口（合成标注数据）
internal/cluster/ 分词/掩码、模板匹配、有界聚类、快照持久化
internal/eval/    合成标注数据集 + 纯度/召回指标
internal/server/  HTTP  handlers
samples/          curl 请求样例
```

## 运行

```bash
go build ./...
go test ./...

# 启动服务（-data 为空则纯内存）
go run ./cmd/logcluster -addr :8080 -data /tmp/lc-state.json -capacity 10000

# 运行评估（输出确定性，两次运行逐字节一致）
go run ./cmd/eval
```

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/v1/logs` | 摄入：`{"line": "..."}` 或 `{"lines": [...]}` |
| GET | `/v1/templates` | 全部模板（含版本、计数、历史） |
| GET | `/v1/templates/{id}` | 单个模板 |
| GET | `/v1/stats` | 统计与淘汰日志 |
| POST | `/v1/snapshot` | 立即写快照（未配置 `-data` 返回 409） |

请求样例见 `samples/requests.sh`。摄入响应示例：

```json
{
  "line": "GET /api/v1/orders/1001 completed in 12ms status=200",
  "template_id": 1,
  "pattern": "GET /api/v1/orders/<NUM> completed in <NUM> status=<VAL>",
  "version": 1,
  "created": true
}
```

## 评估方法与结果

`cmd/eval` 用固定种子（42）生成带标注合成数据，输出**确定性**结果：

- **场景 1（混合流）**：11 个基础模板 ×100 条 + 2 个稀有模式（各 2 条）+
  2 条 200KiB 超长行，共 1106 条、14 个真实标签。其中
  `conn.timeout`/`conn.refused`、`disk.read`/`disk.write` 是同长度、同前缀、
  仅关键字不同的"陷阱对"，专测过度泛化。
- **场景 2（容量淘汰）**：10 个热点模板 ×30 轮 + 20 个一次性冷模板，
  容量 12，验证 LRU 只淘汰冷模板、热点聚类不受影响。
- 指标：成对（pairwise）precision/recall/F1 与加权纯度。

实测输出（`go run ./cmd/eval`，两次运行 diff 为空）：

```
== scenario 1: mixed stream, default capacity ==
lines=1106 clusters=14 labels=14 precision=1.0000 recall=1.0000 f1=1.0000 purity=1.0000 (tp=54453 fp=0 fn=0 tn=556612)
  conn.refused   -> #5  connection error: refused by remote host
  conn.timeout   -> #9  connection error: timed out after <NUM>      # 关键字差异未合并
  disk.read      -> #3  disk sda read error at sector <NUM>
  disk.write     -> #6  disk sda write error at sector <NUM>
  kernel.panic   -> #12 kernel panic: ... at <HEX>                   # 稀有模式(2条)独立成模
  trace.long     -> #13 trace frame#0:... <TRUNC>                    # 超长行截断后正常聚类
  ...

== scenario 2: capacity eviction ==
capacity=12 ingested=320 live_templates=12 evictions=18
lines=320 clusters=30 labels=30 precision=1.0000 recall=1.0000 f1=1.0000 purity=1.0000
evicted template ids: [11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28]   # 全部为冷模板
```

## 验证记录（实际执行）

| 命令 | 结果 |
|---|---|
| `go vet ./...` | 通过 |
| `go test ./...` | 全部通过（cluster / eval / server 三个包） |
| `go run ./cmd/eval` 两次 + `diff` | 输出逐字节一致（确定性） |
| 服务冒烟（curl 摄入/查询/快照/重启恢复） | 通过，重启后 `ingested` 与模板状态完整恢复 |

开发过程中曾出现并已修复的问题（如实记录）：

1. 初版 `TestTokenizeTruncation` 预期写错（字节上限先于 token 上限生效）——修正测试为分别覆盖两种截断。
2. 初版指标未计算 TN —— 已补上。
3. 初版掩码顺序把 8 位以上十进制数误判为 `<HEX>` —— 调整数字先于十六进制。
4. 初版淘汰场景用 30 模板轮询容量 10，所有模板反复重建、指标全 0，
   不能说明问题 —— 改为"热点 + 冷一次性"场景。

当前无未通过项。
