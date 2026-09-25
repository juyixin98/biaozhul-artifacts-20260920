# 短语位置检索（Phrase Position Search）

纯 Java 实现的**带词位置的短语检索**库 + JSON HTTP 服务。无第三方依赖、不调用任何
外部搜索服务或大模型，语料为自建合成语料。使用 JDK 21 内置 `com.sun.net.httpserver`
提供 HTTP 服务，自带一个约 400 行的极简 JSON 解析/序列化器和一个零依赖测试框架。

---

## 1. 它做什么

给定一个短语（多个词）和一个固定的 `slop`，在每篇文档中找出该短语的全部出现，
返回**命中文档以及每个词实际落在的位置**（全局位置、字段内位置、字符偏移、所属字段）。

支持：

- **精确短语**（`slop=0`，词必须相邻）与**固定 slop 的有序近邻短语**；
- **重复词**短语（如 `that that`、`had had`）——同一位置绝不被两个词项复用；
- **跨字段短语**——一篇文档的多个字段按声明顺序拼成一条全局位置流，短语可跨越
  字段边界命中；也支持限定单字段；
- **停用词位置语义可显式选择**（保留占位 / 删除留洞），两种模式都有测试锁定。

## 2. 核心语义（验收口径）

### 2.1 位置

- 分析器把字段文本切成带 **0 基位置号** 和字符偏移的 token。
- 单字段短语使用字段内位置；跨字段短语把文档各字段**按声明顺序**拼接成一条全局
  位置流，字段之间**不加额外空隙**（字段 B 第一个 token 的全局位置 =
  字段 A 消耗的位置槽总数）。

### 2.2 slop（固定、有序）

对短语词项序列 `t0 … t(n-1)`，在某篇文档中选择位置 `p0 … p(n-1)`，命中当且仅当

```
p0 < p1 < … < p(n-1)                                   （严格递增）
slopUsed = (p(n-1) - p0) - (n - 1) ≤ slop
```

- `slop=0`：经典相邻精确短语；
- `slop=k`：允许词项之间总共插入至多 `k` 个其他词（各相邻间隙的多余量之和 ≤ k）；
- **不允许乱序、重叠**；`slopUsed` 在响应中逐条返回，可核对。

### 2.3 重复词不能复用同一位置

严格递增条件保证：即使短语里同一个词出现多次，两次出现也必须落在**两个不同的位置**。
例如三个连续的 `had had had`，查询 `had had` 只会产生 `(0,1)` 和 `(1,2)` 两次命中，
不存在“两个词项共用位置 1”的命中。测试 `everyMatchUsesDistinctPositions` 对多条
重复词短语 × 多个 slop 逐条断言“位置两两不同且严格递增”。

### 2.4 停用词是否保留位置（明确结论）

提供两个分析器，结论都通过测试固定下来：

| 分析器 | 停用词处理 | 位置 |
|---|---|---|
| `standard`（默认） | **保留停用词**，与普通词无差别 | 停用词**占据位置**，无空洞 |
| `stop_gap` | 删除停用词 | **位置不收紧**，被删词留下位置“洞”（与 Lucene StopFilter 一致） |

`stop_gap` 示例：`"the quick brown fox"`（the 为停用词）→
`the` 删除但消耗位置 0，`quick/brown/fox` 位于 1/2/3。
于是短语 `quick fox` 的位置差为 2（中间隔着 the 的洞），`slop=0` 不命中，
`slop=1` 才命中。跨字段拼接时尾部被删的停用词同样计入位置槽（`positionCount`），
保证后一字段基址不错位。

> 设计取舍：默认选择“停用词保留占位”，因为它语义最简单、对用户最直观（第 N 个
> 词的位置永远是 N-1）；`stop_gap` 用于演示并验证“删除但留洞”这一业界常见语义。
> 项目**没有**实现“删除并收紧位置”的模式——那种模式会让位置与原文对不上，
> 刻意不提供。

## 3. 目录结构

```
src/main/java/com/example/phrasesearch/
  analyze/      Analyzer 接口、StandardAnalyzer、StopGapAnalyzer
  model/        Document / Token / Posting / IndexedDoc / GlobalToken …
  index/        Index：内存倒排表（term -> 带位置/字段/偏移的 Posting 列表）
  search/       Searcher（倒排表 + DFS 枚举）、BruteForce（穷举参考实现）
  query/        PhraseQuery、PhraseMatch
  service/      PhraseService：查询分析、检索、JSON 响应组装
  server/       PhraseHttpServer：JDK HttpServer，JSON 接口
  json/         零依赖 JSON 解析/序列化
  corpus/       SyntheticCorpus：内置合成语料
  Main.java     入口；CorpusLoader.java：外部 JSON 语料加载
src/test/java/…  零依赖测试（Assert 小框架 + AllTests 入口）
scripts/        build.sh / run_tests.sh / run_server.sh
examples/       requests.http、curl-examples.sh、output/（真实响应留存）
data/           sample-corpus.json（外部语料格式样例）
REPORT.md       实际构建/运行/测试记录（含未通过项的如实记录）
```

## 4. 构建与测试（仅需 JDK 21，无需 Maven/Gradle/联网）

```bash
bash scripts/build.sh        # 编译到 build/
bash scripts/run_tests.sh    # 编译并运行全部自动化测试
```

测试规模：**3152 个断言全部通过**，0 失败（见 `REPORT.md`）。

测试覆盖：

- **分析器**：切词、大小写、偏移；两种停用词位置语义；尾部停用词的位置槽。
- **检索语义**：精确短语、slop 边界穷举、重复词（that/had/alpha）、跨字段边界、
  字段限定、单 token、偏移正确性、slop 单调性、位置两两不同不变量。
- **穷举小序列参考（重点验收项）**：`BruteForce` 对每个文档做**完整笛卡尔积**
  枚举（独立于 `Searcher` 的 DFS+剪枝实现）。测试在手工构造的小序列上对
  所有查询 × slop 0..3 × 字段范围做全枚举比对，再做 **2000+ 组固定种子随机差分**
  （standard 与 stop_gap 各 1000 左右，含大量重复词），逐字段比较位置、
  slopUsed、crossField，两个实现结果（含顺序）必须完全一致。
- **JSON**：解析/序列化往返、转义、数字、错误输入。
- **HTTP 端到端**：真实起服务，GET/POST、状态码、跨字段与重复词响应、400 错误。

## 5. 启动服务

```bash
bash scripts/run_server.sh                           # 默认 8080，standard 分析器
bash scripts/run_server.sh --port 8099 --analyzer stop_gap
bash scripts/run_server.sh --corpus data/sample-corpus.json
```

## 6. JSON 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /config` | 分析器、停用词表与位置策略、slop/跨字段/重复词语义说明 |
| `GET /docs` | 全部文档的全局 token 流与每个 token 的位置（核对用） |
| `GET/POST /search` | 短语检索，参数 `query`(必填)、`slop`(默认0)、`field`(可空=跨字段) |
| `GET/POST /analyze` | 参数 `text`，返回词项/位置/偏移 |

`/search` 请求：

```json
{ "query": "that that", "slop": 5 }
```

响应（节选，完整样例见 `examples/output/`）：

```json
{
  "query": "that that",
  "analyzer": "standard",
  "terms": ["that", "that"],
  "slop": 5,
  "field": "_all_cross_field",
  "totalDocsMatched": 1,
  "totalOccurrences": 8,
  "documents": [
    { "docId": "doc3", "occurrences": 8,
      "matches": [
        { "slopUsed": 0, "crossField": false, "spanStart": 0, "spanEnd": 1,
          "positions": [
            {"term":"that","field":"title","globalPosition":0,"positionInField":0,"startOffset":0,"endOffset":4},
            {"term":"that","field":"title","globalPosition":1,"positionInField":1,"startOffset":5,"endOffset":9}
          ]},
        "..."
      ]}
  ]
}
```

错误请求（缺 `query`、负 `slop`、未知字段、非法 JSON）返回 HTTP 400 与
`{"error":true,"message":...}`。

请求样例：

- `examples/requests.http`（IntelliJ / VS Code REST Client 可直接执行）
- `examples/curl-examples.sh`（纯 curl，逐条可复制）
- `examples/output/`：本次实际运行保存的真实 JSON 响应。

## 7. 内置合成语料（8 篇，字段顺序均为 title→body）

| id | 用途 |
|---|---|
| doc1 | 普通精确短语 `quick brown fox`（title、body 各一次） |
| doc2 | 跨字段：title 尾 `phrase` ↔ body 头 `search` |
| doc3 | **重复词** `that that`（连续 + 多个非连续出现，用于穷举） |
| doc4 | **重复词**三连 `had had had` + body 中多个 had |
| doc5 | **跨字段 + 停用词**：title 尾 `system`，body 头 `the quick` |
| doc6 | 重复 `alpha/beta` 的距离穷举序列 |
| doc7 | 跨字段边界差一个词的 slop 边界 |
| doc8 | 不含目标词的干扰文档 |

外部语料格式见 `data/sample-corpus.json`：
`{"documents":[{"id":"x","fields":{"title":"…","body":"…"}}]}`（字段按对象内顺序拼接）。

## 8. 实现说明与取舍

- **倒排表 + DFS**：`term -> List<Posting>`，检索时取各词项在同一文档的候选位置，
  深度优先、严格递增地枚举组合，并做“为后续词项预留最小间隙”的安全剪枝。
- **穷举 oracle**：`BruteForce` 不做任何优化，直接笛卡尔积后逐条判条件，作为
  语义基准；生产代码不依赖它，但测试用它给 `Searcher` 兜底。
- **纯 JDK**：不引入 Jackson/JUnit/任何框架，`javac/java` 即可构建运行，
  便于离线复现。
- **局限（如实说明）**：内存索引、单机单进程；线程模型简单；未做高亮/打分；
  slop 固定为“有序”语义（不支持词项换位）；中文等无空格语言按 Unicode
  字母/数字连续段切词，不做分词。
