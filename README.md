# 短语位置检索（phrase-search）

纯后端、零外部依赖的本地文本短语检索库 + JSON HTTP 服务。**不调用任何外部搜索服务、不调用大模型**；语料为代码内自建合成语料；只依赖 JDK 21 标准库（HTTP 用 JDK 内置 `com.sun.net.httpserver`，JSON 为自实现的最小解析/序列化器）。

## 1. 功能与关键定义

### 1.1 含词位置的短语查询

- 分词器按 Unicode 字母/数字切词，整体转小写（`Locale.ROOT`），**位置从 1 开始、连续递增；标点不产出词元也不占位置**。
- 索引在每个字段内记录 `(词项 → 有序位置列表)`，并把一篇文档的多个字段按固定字段顺序拼接成一条**全局位置序列**，保存每个全局位置所属的字段。
- 短语查询是有序词项序列，命中要求为**严格递增**的位置序列 `p0 < p1 < ... < pn-1`。因此：
  > **重复词不能重复使用同一个位置。** 例如某词在文档中只出现 1 次，查询 `w w` 无论 slop 多大都不命中；出现 2 次时 `w w` 只有一种命中 `(p1,p2)`。
- 返回匹配的**文档 id、每次命中的实际全局位置、逐词所属字段、起止位置、是否跨字段**。

### 1.2 slop（固定定义）

> **slop = 相邻两个匹配词之间允许出现的“其他词（空位）”的最大数量。**

形式化：对所有相邻词项要求 `1 <= p(i+1) - p(i) <= slop + 1`。

| slop | 含义 | `a b` 对 `a x x b`（差 3） |
|---|---|---|
| 0 | 精确相邻短语（位置差恰为 1） | 不命中 |
| 1 | 中间至多 1 个词 | 不命中 |
| 2 | 中间至多 2 个词 | 命中（位置 1,4） |

### 1.3 停用词是否保留位置 —— 本项目的明确结论

**保留位置（position with gaps）。** 提供两个分析器：

- `standard`：不分停用词；
- `stopword`：删除固定停用词表 `{a, an, the, of, is, and, in, to, on, for}` 中的词，
  **但其余词保留它们在原文本中的位置编号**，被删词的位置形成“空位”。

`the cat sat in the mat` 经 stopword 分析器得到：`cat@2, sat@3, mat@6`（位置 1、4、5 是空位）。

这一决策的后果（也是验收点）：在 stopword 索引上查 `cat mat`，`cat@4 → mat@8` 的位置差为 4，需要 **slop >= 3** 才命中；slop 0/1/2 均不命中。空位仍占据真实距离，因此 slop 语义不会因为删词而被悄悄改变。查询串中的停用词同样删除（查询词位减少），距离由索引空位体现；若查询经分析后没有任何词（只含停用词），返回 400。

### 1.4 跨字段短语与 fieldGap

- 文档字段按插入顺序拼接为全局序列；查询默认在**全字段序列**上匹配，因此短语可以跨字段（响应中 `crossField: true` 并给出每个位置的字段名）。
- 跨字段时两字段之间插入 `fieldGap` 个**虚拟位置**（默认 **0**：前一字段末词与后一字段首词视为紧邻）。
  - 默认 gap=0：d2 的 `data@8(body) → pipeline@9(tags)` 用 `data pipeline`、slop=0 即命中；
  - gap=1：d7 的 `blue@2(title) → ocean@4(body)` 位置差 2，`blue ocean` 需要 slop>=1；
  - 可用 `field` 参数把匹配限制在单个字段内（跨字段命中被排除）。

### 1.5 匹配算法（不是“贪心取最早”）

`PhraseMatcher` 按查询词顺序 DFS，每层在该词的有序位置表上用二分查找选候选，并做可行性剪枝，**枚举全部合法位置组合**，输出按位置元组字典序排列。

项目另含 `BruteForceReference`：对候选位置做**笛卡尔积穷举后逐一过滤**，作为小序列参考实现。生产实现在 300 组随机小序列 + 全部语料×查询×slop×fieldGap 组合上与蛮力参考**逐元组相等**（见 `BruteEquivTest`）。

为什么不能贪心：序列 `x x y` 上查 `x y` slop0，若第一个词只贪心固定在最早的位置 1，会错误报告“无匹配”；实际上位置 2 的 x 与 y 相邻，命中 `(2,3)`。slop1 时更有 `(1,3)`、`(2,3)` 两个解，枚举算法两者都返回。

每文档匹配数有上限（默认 100，可配），超过时响应中带 `truncated: true`。

## 2. 合成语料

硬编码于 `src/main/java/phrase/doc/Corpus.java`，共 7 篇（字段顺序固定）：

| id | title | body | tags | 构造意图 |
|---|---|---|---|---|
| d1 | `echo chamber` | `we echo echo echo echo today` | — | 重复词 echo 多次，同字段枚举全部相邻对，且可跨字段 |
| d2 | `infra notes` | `we build a pipeline for data` | `pipeline streaming` | 跨字段短语 `data pipeline`（body→tags） |
| d3 | `rain watch` | `the rain brings more rain` | — | 重复词 rain 跨/不跨字段，slop 边界 |
| d4 | `pattern` | `alpha alpha beta` | — | 重复词 + 贪心陷阱序列 |
| d5 | `the cat` | `the cat sat in the mat` | — | 停用词空位与 slop 的关系 |
| d6 | `aba` | `a b a` | — | 回指型重复词，穷举枚举参考 |
| d7 | `deep blue` | `ocean currents` | — | fieldGap 跨字段边界（字段首尾词） |

## 3. 目录结构

```
src/main/java/phrase/
  core/        Token, Analyzer, StandardAnalyzer, StopwordAnalyzer, Analyzers
  doc/         Doc, Corpus（合成语料）
  index/       Occ, InvertedIndex（倒排索引、全局位置、字段偏移）
  search/      PhraseQuery, Match, DocMatch, SearchResult,
               PhraseMatcher（DFS+二分枚举）, BruteForceReference（蛮力穷举）,
               SearchService（双分析器索引、AND 预过滤、字段限制、截断）
  json/        Json（零依赖 JSON 解析/序列化）
  server/      ApiServer（JDK HttpServer，/search /analyze /corpus /health）
  Main.java    CLI：serve / search
src/test/java/phrase/test/   40 个自动化测试（自研断言框架，无 JUnit）
scripts/       build.sh, test.sh
examples/      请求样例、真实响应样例、curl 脚本
```

## 4. 构建与运行

要求 JDK 21（仅用 `javac/java`，无需 Maven/Gradle，无需网络）。

```bash
bash scripts/build.sh                 # 编译到 build/classes
bash scripts/test.sh                  # 编译并运行全部自动化测试
```

### 4.1 启动 JSON 服务

```bash
java -cp build/classes phrase.Main serve --port 8080 --field-gap 0 --max-matches 100
#   --port        监听端口，默认 8080
#   --field-gap   字段间虚拟位置数，默认 0
#   --max-matches 每文档最多返回的匹配数，默认 100
```

### 4.2 CLI 一次性检索（无需起服务）

```bash
java -cp build/classes phrase.Main search "echo echo" --slop 0
java -cp build/classes phrase.Main search "data pipeline" --slop 0
java -cp build/classes phrase.Main search "cat mat" --analyzer stopword --slop 3
java -cp build/classes phrase.Main search "blue ocean" --slop 1 --field-gap 1
java -cp build/classes phrase.Main search "data pipeline" --field body   # 字段限制
# 也可绕过查询文本直接给词项：--terms alpha,alpha,beta
```

## 5. HTTP JSON API

所有请求/响应均为 `application/json; charset=utf-8`。错误统一为 `400`（请求问题）/`404`（路径不存在），响应形如 `{"error":true,"status":400,"message":"..."}`。

### POST /search

请求字段：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `query` | string | 与 `terms` 二选一 | — | 查询文本，由所选分析器分词 |
| `terms` | string[] | 与 `query` 二选一 | — | 直接给词项（会做 trim+小写校验，只允许字母数字） |
| `slop` | int | 否 | 0 | 相邻词允许的最大间隔词数，范围 0..1000 |
| `analyzer` | string | 否 | `standard` | `standard` 或 `stopword` |
| `field` | string | 否 | 全字段 | 限定单字段（如 `body`/`title`/`tags`） |

响应：`queryTerms`（实际参与匹配的词项）、`totalHits`、`truncated`，以及 `hits[]`：每篇命中文档含 `docId`、`matchCount`、`truncated` 和 `matches[]`；每个 match 含 `positions`（逐词全局位置）、`fields`（逐词字段名）、`start`、`end`、`crossField`。命中按匹配数降序、文档 id 升序排列。

```bash
curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"data pipeline","slop":0}'
```

### POST /analyze

查看分析器输出（词项 + 1 基位置），用于核对停用词空位：

```bash
curl -s -X POST http://127.0.0.1:8080/analyze \
  -H 'Content-Type: application/json' \
  -d '{"text":"the cat sat in the mat","analyzer":"stopword"}'
# tokens: cat@2, sat@3, mat@6
```

### GET /corpus、GET /health

返回合成语料全文 / `{"status":"ok"}`。

`examples/requests/` 有 6 个请求样例，`examples/responses/` 是从实际运行服务抓取的对应响应，`examples/curl-examples.sh` 可一键复现。

## 6. 自动化测试与验收对照

`bash scripts/test.sh` 运行 **40 个测试**（实际输出见 `RUNLOG.md`）：

- 分词/分析器 7 个：位置编号、标点不占位、停用词删除后**位置保留**（cat@2,sat@3,mat@6）；
- 匹配器 8 个：单词全部位置、slop0/slop1/slop2 边界、**重复词不重用位置**、`x x y` 贪心陷阱、`a b a b a` 穷举元组数、结果上限截断、缺词不命中；
- 蛮力等价 2 个：300 组随机小序列 + 全语料组合，DFS 实现与笛卡尔积穷举参考**逐元组一致**；
- 端到端 10 个：d1 重复 echo 枚举、d2 跨字段实际位置 (8,9)/字段标签、fieldGap 0/1/2 对跨字段的影响、字段限制、停用词 slop 边界（cat mat 需 slop3）、查询只含停用词报错等；
- JSON 5 个：嵌套往返、转义、数字/字面量、解析错误、键顺序；
- HTTP API 8 个：经真实回环连接验证 /health、/corpus、/search（重复词/跨字段/停用词/terms 数组）、/analyze、400/404/方法错误。

验收点对应：

| 验收要求 | 落点 |
|---|---|
| 构造重复词 | d1（echo×4）、d3（rain×3）、d4（alpha alpha beta）、d6（a b a） |
| 构造跨字段短语 | d2（data@8 body → pipeline@9 tags）、d7（fieldGap 边界） |
| 穷举小序列参考 | `BruteForceReference` + `BruteEquivTest`（随机+全组合逐元组比对）、`a b a b a` 手算元组 |
| 明确停用词是否保留位置 | 保留：`StopwordAnalyzer` 注释、/analyze 输出、`cat mat` 需 slop3 的测试 |
| 重复词不重用同一位置 | positions 严格递增；单词出现时 `w w` 任何 slop 不命中的测试 |
| 返回匹配文档和实际位置 | 响应 docId + positions + fields + start/end + crossField |
| 实际运行并如实记录 | `RUNLOG.md`（命令、结果、失败与修正过程） |

## 7. 非目标

不做前端页面；不做持久化（进程内内存索引）；不接入任何网络检索/嵌入/大模型服务；停用词表为内置固定表。
