# 布尔检索查询计划（Boolean Retrieval Query Planner）

一个**纯后端、零外部依赖**的本地文本布尔检索库 + JSON HTTP 服务。
不调用任何外部搜索服务、不调用大模型；语料为代码内自建的合成语料。
仅依赖 JDK（开发与验证使用 JDK 21，未使用任何第三方库，不需要网络）。

## 功能

- **布尔查询解析**：支持 `AND`、`OR`、`NOT` 与括号，优先级 `NOT > AND > OR`。
  - 关键字大小写不敏感（`and/And/AND`），另接受符号 `& | - !` 与全角括号 `（）`。
  - 语法错误**精确保留字符位置**（0 基偏移），不做猜测式修复。
- **NOT 相对于固定文档全集**：`NOT x = 当前存活文档全集 − x 的倒排链`；
  删除文档会使全集同步缩小（软删除，倒排链不过期，查询时过滤）。
- **交集顺序优化**：对每个 AND 的合取项，按倒排链（结果集）大小**从小到大**
  排序后再依次交集；同一次查询同时给出"朴素计划"与"优化计划"两份结果、
  两份工作量计数与可读执行轨迹，便于对照。
- **未知词**：倒排链为空集，参与正常集合运算（`x AND 未知 = 空`、
  `x OR 未知 = x`、`NOT 未知 = 全集`）。
- **JSON 服务**：JDK 内置 `com.sun.net.httpserver`，手写最小 JSON 解析/序列化。

## 目录结构

```
src/main/java/booleansearch/
  model/Document.java            文档记录（id/title/text）
  index/Tokenizer.java           分词器（ASCII 字母数字，小写化）
  index/InvertedIndex.java       倒排索引、软删除、NOT 固定全集
  query/QueryLexer.java          词法分析
  query/QueryParser.java         递归下降语法分析（AND/OR/NOT/括号）
  query/QueryNode.java           AST（sealed interface + record）
  query/QueryParseException.java 解析异常（携带字符位置）
  search/NaiveEvaluator.java     朴素左结合求值（优化前基线）
  search/OptimizedPlanner.java   按倒排大小排序交集（优化后）
  search/SearchEngine.java       门面：一次查询跑两版计划并对照
  search/EvalResult.java         命中文档 + 工作量计数 + 轨迹
  corpus/SyntheticCorpus.java    自建合成语料（12 篇，受控词频）
  json/Json.java                 零依赖 JSON 解析/序列化
  server/SearchHttpServer.java   HTTP JSON 服务
  Main.java                      启动入口
src/test/java/booleansearch/     六个测试套件（自带迷你测试框架）
samples/requests/                请求样例（curl --data @file）
samples/responses/               实际运行抓取的响应样例
build.sh / test.sh / run.sh      构建 / 测试 / 启动脚本
RUN_REPORT.md                    实际运行的命令与结果记录
```

## 构建与运行

需要 JDK（验证环境为 OpenJDK 21）。无需 Maven/Gradle。

```bash
./build.sh          # 编译到 build/classes
./test.sh           # 编译并运行全部自动化测试（退出码 0 = 全过）
./run.sh            # 启动 HTTP 服务，默认 http://127.0.0.1:8080
./run.sh --port 9090 --host 127.0.0.1
```

等价的裸命令：

```bash
mkdir -p build/classes
find src/main/java -name '*.java' > build/main-sources.txt
javac -encoding UTF-8 -d build/classes @build/main-sources.txt
java -Dfile.encoding=UTF-8 -cp build/classes booleansearch.Main --port 8080
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活检查 |
| GET | `/stats` | 索引统计（存活/删除文档数、词项数） |
| GET | `/documents` | 存活文档列表 |
| POST | `/search` | 入参 `{"query": "..."}`，执行布尔检索 |
| POST | `/documents` | 入参 `{"title": "...", "text": "..."}`，添加文档 |
| DELETE | `/documents/{id}` | 软删除文档（NOT 全集随之缩小） |

错误统一形如 `{"error": "...", "position": 12}`，`position` 为查询串（或 JSON
请求体）中的 0 基字符偏移；与位置无关的错误为 `-1`。

### 查询示例

```bash
curl -s http://127.0.0.1:8080/stats

curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  --data @samples/requests/search_and_order.json

curl -s -X POST http://127.0.0.1:8080/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "(cat OR dog) AND NOT quantum"}'

curl -s -X POST http://127.0.0.1:8080/documents \
  -H 'Content-Type: application/json' \
  --data @samples/requests/add_document.json

curl -s -X DELETE http://127.0.0.1:8080/documents/11
```

`/search` 响应同时包含两版计划（节选）：

```json
{
  "query": "pet AND cat AND zebra",
  "normalizedAst": "(pet AND cat AND zebra)",
  "hitCount": 0,
  "resultsIdentical": true,
  "naivePlan":     { "membershipProbes": 12, "trace": [ "...7 ∩ 6，探测 7 次...", "...5 ∩ 1，探测 5 次..." ] },
  "optimizedPlan": { "membershipProbes": 1,  "trace": [ "...排序后第 1 位: zebra（倒排大小=1）...", "...1 ∩ 6，探测 1 次 -> 0 篇..." ] },
  "costComparison": {
    "naiveMembershipProbes": 12,
    "optimizedMembershipProbes": 1,
    "probeDeltaOptimizedMinusNaive": -11
  }
}
```

完整真实响应见 `samples/responses/`。

## 语义约定

- **分词**：连续 ASCII 字母/数字序列（含下划线）为一个词，统一小写；
  标题与正文一起索引。中文标题不作为可检索词（正文为英文受控词表），
  非字母字符出现在查询串中会报"无法识别的字符"并给出位置。
- **文法**：`orExpr := andExpr (OR andExpr)*`、
  `andExpr := notExpr (AND notExpr)*`、`notExpr := NOT notExpr | atom`、
  `atom := TERM | '(' orExpr ')'`。`NOT NOT a` 合法（双重否定）。
- **NOT 全集**：`index.liveDocIds()` = 曾装入的全部文档减去被删除文档；
  与查询中其他词项无关，固定且可复现。
- **优化规则**：仅对 AND 的直接合取项重排。词项用倒排链长度估计代价；
  括号/OR/NOT 子表达式先求值得到真实大小再参与排序。估计值相同保持原序。
  交集算法为"遍历累加器、对右侧集合做 contains 探测"，因此**成员探测次数**
  是与顺序相关、可公平比较的工作量指标（OR 并集与 NOT 差集的工作量不被优化改变）。

## 合成语料

`SyntheticCorpus` 内置 12 篇短文档，词频刻意分三档（df=出现文档数）：

- 常见：`pet`(7)、`cat`(6)、`dog`(5)、`food`/`coffee`/`tea`(各4)
- 中等：`city`/`garden`(各3)、`quantum`/`robot`/`water`(各2)
- 稀有（df=1）：`zebra`、`xylophone`、`kayak`、`falcon`、`glacier`、`neon`、`park`

使 AND 查询的链大小差异明显，优化前后探测次数差距可观察（例如
`pet AND cat AND zebra`：朴素 12 次 vs 优化 1 次）。

## 自动化测试与验收

`./test.sh` 运行六个套件：

1. **ParserTest**：合法查询 AST 规范化；14 类语法错误的位置精确断言。
2. **IndexTest**：分词、增删恢复、倒排链过滤、固定全集。
3. **JsonTest**：JSON 往返、record 序列化、整数/小数类型、错误位置。
4. **ExhaustiveEquivalenceTest（验收核心）**：4 篇文档的文档-词矩阵，
   穷举 **16 种删除子集 × 588 种查询形态 = 9408 个组合**，每个组合三方对照：
   朴素求值、优化求值、**独立真值**（不经本项目索引/求值器，直接由
   文档-词矩阵做 JDK 集合运算），并点名覆盖纯 NOT、未知词、删除文档。
5. **OptimizationCostTest**：真实语料上优化前后探测次数的精确对照。
6. **SearchServerTest**：真实启动 HTTP 服务发请求的端到端测试。

最近一次完整运行：**37,761 个断言，0 失败，退出码 0**；
9,408 个穷举组合全部与独立真值一致；朴素计划成员探测 9,024 次，
优化后 5,868 次（节省 3,156 次），其中 2,190 个组合严格减少。
详见 [RUN_REPORT.md](RUN_REPORT.md)（命令、输出、未通过项如实记录）。

## 不做前端

本项目仅提供 JSON HTTP 接口与命令行构建脚本，不含任何页面/UI。
