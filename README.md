# 日志模板在线聚类（logcluster）

一个纯后端、零第三方依赖（Go 标准库）的**可观测性日志处理**样例服务。它在摄入日志时
**在线**地把自由文本日志行归纳为有界数量的**模板（template）**，抽取数字 / UUID / 字符串
变量，同时**保留关键字差异**，避免把不同错误过度泛化合并。模板的每次放宽都产生一个**带时间戳
和原因的新版本**。存储全部有界：模板数、事件数、单行长度都有上限，超容量按 LRU 淘汰。

使用**合成数据**，不依赖任何真实监控平台。无前端。

---

## 1. 它解决什么问题

同一类错误可能打印成：

```
2026-09-24 10:00:00 WARN db: Connection to 10.0.0.1:5432 refused: dial tcp, retrying
2026-09-24 10:00:01 WARN db: Connection to 10.0.0.2:6001 refused: dial tcp, retrying
```

应归纳为**一个**模板，变量位置用占位符表示：

```
<NUM>-<NUM>-<NUM> <NUM>:<NUM>:<NUM> WARN db: Connection to <NUM>.<NUM>:<NUM> refused: dial tcp, retrying
```

但下面两行**不能**合并——关键字 `refused` 与 `timed out` 区分了两种不同故障：

```
... Connection to 10.0.0.1:5432 refused: dial tcp, retrying
... Connection to 10.0.0.3:5432 timed out after 500ms, giving up
```

本项目的核心约束就是：**抽取变量，但不吞掉关键字**。

---

## 2. 构建与运行

需要 Go 1.22+（开发环境为 `go1.22.2 linux/amd64`）。

```bash
make build           # 产出 bin/logcluster（HTTP 服务）与 bin/eval（评估器）
make test            # 全部单元/集成测试
make eval            # 跑标注合成语料，输出纯度/召回并作为验收门禁
make demo            # 端到端 HTTP 演练（LRU 淘汰、查询、快照重启）
```

启动 HTTP 服务：

```bash
./bin/logcluster -addr :8080 -snapshot data/snapshot.json
```

主要启动参数：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `-addr` | `:8080` | 监听地址 |
| `-snapshot` | `data/snapshot.json` | 本地 JSON 快照路径（启动自动加载、退出/定时保存） |
| `-max-clusters` | `500` | 活跃模板上限（容量边界），超限 LRU 淘汰 |
| `-ring-capacity` | `5000` | 留存事件上限（环形缓冲，满则覆盖最旧） |
| `-max-line-bytes` | `16384` | 单行保留字节上限（超长行裁剪并计入截断指标） |
| `-autosave` | `30s` | 定时快照间隔，`0` 关闭 |

接口与 `curl` 样例见 [`examples/API.md`](examples/API.md)，请求体见
[`examples/ingest-one.json`](examples/ingest-one.json) 与
[`examples/ingest-batch.json`](examples/ingest-batch.json)。

---

## 3. 聚类算法（确定性在线聚类）

每条日志行进过一条固定流水线；**同样的输入流永远产出同样的簇 ID、模板与版本号**。

### 3.1 分词 `internal/tokenize`

把一行切成带类型的槽位（slot）：

- `<NUM>`：数字，支持十进制 / 浮点 / 指数 / 十六进制，以及短单位后缀（`12ms`、`99%`、`0xDEADBEEF`）。
  带符号数字（`-7`）只在**行首或空白后**识别，因此日期 `2026-09-24` 中的 `-` 仍是标点。
- `<UUID>`：8-4-4-4-12 十六进制 UUID（支持裸值、`{...}`、`urn:uuid:` 前缀）。
- `<STR>`：成对单/双引号包裹的字符串。
- 其余字母词与标点都是**字面量**。纯字母关键字（`refused`、`timeout`、`NullPointerException`）
  永远不会被当作变量。
- 单个行最多产生 `MaxTokens=2048` 个槽位；超长行在此截断并标记 `Truncated`。

每个槽位还记录其前面是否有空白，渲染模板时据此**忠实还原原始间距**（`GET /api/users/<NUM>`）。

### 3.2 匹配与分配 `internal/cluster` + `internal/engine`

新行到来时，按簇 ID 顺序：

1. **精确匹配**：槽位数相同，且每个槽位满足——字面量必须完全相同；`<NUM>/<UUID>/<STR>`
   只接受对应类型；`<*>` 接受任意单个槽位。
2. **可修复的近失（healable near-miss）**：恰好一个位置不匹配，且新旧值都是“像值的词”
   （含数字、长度受限，如 `task101` vs `task102`）。此时把该槽位**提升（promote）为通用
   `<*>` 变量**并追加一个**新版本**。
3. 都不满足则**新建模板**；模板数已达上限时，先按 **LRU 淘汰**最久未使用的模板。

“避免过度泛化”由两条硬规则保证：

- 纯字母关键字差异（`refused`/`timed out`、两种异常类名）既不匹配也不可提升 → 保持不同模板；
- 类型不匹配（在 `<NUM>` 位置出现 UUID）不匹配 → 不会悄悄合并。

### 3.3 模板版本

每个簇保存完整版本历史 `versions[]`：版本号、渲染后的模板、时间戳、变更原因。例如
`G6-wordparam` 先以字面值 `task101` 建版（v1），见到 `task102` 后该槽位提升为 `<*>`（v2）。

### 3.4 有界性与持久化

- **模板**：`-max-clusters` 个，LRU 淘汰，淘汰记录进入有界的 `/v1/evicted` 日志。
- **事件**：固定容量环形缓冲（`internal/ring`），满则覆盖最旧；查询最新优先。
- **样例**：每簇最多保留 3 条去重示例。
- **单行**：字节上限裁剪 + 槽位上限截断；两者都计入 `truncated_lines`。
- **快照**：`internal/engine/persist.go`，确定性顺序序列化 JSON，临时文件 + 原子 rename；
  启动自动加载。进程重启后模板、版本历史、事件与计数都能恢复。

并发安全：所有写路径在互斥锁内，事件环形缓冲自带读写锁；`go test -race` 通过。

---

## 4. 标注合成语料与评估

`internal/synth` 生成 **208 行、14 个标注组**的确定性语料（无随机源，多次构建逐字节相同），
刻意覆盖：

- **数字 / UUID / 引号变量**（HTTP 请求、DB 连接、磁盘、认证）；
- **关键字不同的错误**：`refused` vs `timed out`；`NullPointerException` vs
  `IllegalArgumentException`（标注上就是两个组，**期望它们分开**）；
- **单词参数提升**：`task101/task102/task103` → 模板演进到 v2 `<*>`；
- **超长行**：约 30KB、2400 个 `fieldN=v` 片段（超过 2048 槽位上限，验证截断且仍归为一簇）；
- **稀有模式**：仅出现一次的致命错误，保持独立单例簇；
- **容量淘汰**：见端到端演练，把 `-max-clusters` 调到很小即可复现。

评估指标（`internal/eval`）：

- **纯度 purity** = Σ_c max_g |簇 c ∩ 组 g| / N —— 簇是否只含同一真实组（惩罚错误合并）；
- **召回 recall（逆纯度）** = Σ_g max_c |簇 c ∩ 组 g| / N —— 每个真实组是否被一个簇完整捕获
  （惩罚碎片化）。

### 实际运行结果（本仓库真实执行）

```
lines=208  ground-truth groups=14  produced clusters=14  truncated lines=6
purity=1.0000  recall=1.0000  f1=1.0000  min-group-recall=1.0000
RESULT: PASS
```

- 14 个标注组 ↔ 产出 14 个簇，一一对应；
- 6 条超长行被截断计数，且全部归入同一簇 `c8`；
- 稀有单例 `G13-rare` 独立成 `c14`；
- 单词参数簇 `c6` 为 **v2**（发生过一次提升），其余为 v1；
- `make eval` 以 purity/recall 阈值（默认均为 1.0）作为退出码门禁，未达阈值返回非零。

> 说明：合成语料是“为检验这些性质而设计”的干净数据，因此指标能达到 1.0；它证明的是上述
> 合并 / 不合并规则被正确执行，而不是在真实噪声日志上的精度。真实日志通常达不到满分。

**确定性**：连续两次 `./bin/eval --json` 的簇分配、模板、版本、计数完全一致（已用 `diff` 验证）。

### 性能（超长行回归基准）

开发中发现并修复了一个真实性能缺陷：引号正则交替分支锚点写法
（`^"..."|^'...'`）在 RE2 下使第二个分支变为非锚定，导致对超长行退化为 O(n²)，
单条 30KB 行分词约 **1.6s**。修复（`^(?:"..."|'...')`）后：

```
BenchmarkLongLine-16   ~2.8 ms/op（约 30KB 行），吞吐 ~11 MB/s
```

该基准已固化在 `internal/tokenize` 测试中以防回归；整条 208 行评估现在约 0.03s。

---

## 5. HTTP 接口一览

| 方法与路径 | 作用 |
| --- | --- |
| `GET /healthz` | 存活探针 |
| `POST /v1/ingest` | 摄入单行 / 批量（JSON）或纯文本（每行一条） |
| `GET /v1/templates` | 列出全部活跃模板（按 ID） |
| `GET /v1/templates/{id}` | 单个模板，含完整版本历史 |
| `GET /v1/events` | 查询事件（`q=` 子串、`cluster_id=`、`limit=`），最新优先 |
| `GET /v1/evicted` | LRU 淘汰模板的有界记录 |
| `GET /v1/metrics` | 计数指标（行数、截断、簇数/容量、淘汰数、事件数/容量） |
| `POST /v1/snapshot` | 原子保存快照到给定路径 |

快速试用：

```bash
# 摄入
curl -s -X POST localhost:8080/v1/ingest -H 'Content-Type: application/json' \
  -d @examples/ingest-batch.json | jq

# 看模板（注意 refused 与 timed out、两种异常各成一簇）
curl -s localhost:8080/v1/templates | jq -r '.templates[] | "c\(.id) v\(.version): \(.template)"'

# 按子串查事件
curl -s 'localhost:8080/v1/events?q=refused' | jq

# 指标
curl -s localhost:8080/v1/metrics | jq
```

---

## 6. 目录结构

```
.
├── cmd/
│   ├── logcluster/        # HTTP 服务入口（加载快照、定时/退出保存）
│   └── eval/              # 标注语料评估 CLI（purity/recall 门禁，支持 --json）
├── internal/
│   ├── tokenize/          # 分词：NUM/UUID/STR 变量、字面量、槽位上限、忠实渲染
│   ├── cluster/           # 单模板簇：类型化匹配、近失提升、版本历史
│   ├── engine/            # 在线分配、LRU 容量淘汰、指标、JSON 快照
│   ├── ring/              # 有界事件环形缓冲
│   ├── server/            # net/http JSON API
│   ├── synth/             # 确定性标注合成语料
│   └── eval/              # purity / recall 评分与报告
├── examples/              # 请求样例与 API 文档
├── scripts/demo.sh        # 端到端 HTTP 演练
├── Makefile
└── go.mod
```

---

## 7. 测试

```bash
make test          # 常规
make test-race     # 竞态检测
make bench         # 基准（含超长行回归基准）
go test ./... -v   # 详细
```

覆盖：分词（UUID/数字/单位/引号/日期分隔符/带符号边界/超长截断）、类型化匹配与关键字不合并、
近失提升与版本历史、样本有界、环形 FIFO 淘汰与查询过滤、引擎 LRU 淘汰、确定性重放、空行/超长
裁剪、快照往返恢复、以及完整 HTTP handler（摄入/查询/错误码/快照）和端到端 208 行语料的
purity/recall=1.0 门禁。

## 8. 已知边界与非目标

- 不做多语言自然语言解析、不做嵌入/向量聚类；变量识别基于确定性规则。
- 单机本地持久化样例（JSON 快照），不是数据库，也不处理多副本一致性。
- 无鉴权、无前端；仅用于本地演示与评估。
- 合成语料为干净数据，满分指标用于验证规则正确性，不代表真实日志精度。
