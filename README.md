# 词典分词动态规划（Dictionary-Cost Chinese Segmenter）

纯后端的中文最小代价分词库 + 本地 JSON 服务。基于**词典一元代价**用**动态规划（Viterbi / list-Viterbi）**求最小代价切分，支持 **N 最佳路径**但不枚举指数级候选。未知字符代价明确、同分有确定性决胜规则、支持词典版本切换。

- **无外部依赖**：仅用 JDK（HTTP 服务用 JDK 内置 `com.sun.net.httpserver.HttpServer`，JSON 为自带的最小实现）。
- **不调用任何外部搜索服务或大模型**；语料为仓库内自建合成语料。
- **无前端**。

## 目录

- [环境与构建](#环境与构建)
- [快速开始](#快速开始)
- [算法说明](#算法说明)
- [HTTP JSON 接口](#http-json-接口)
- [请求/响应样例](#请求响应样例)
- [测试与验收](#测试与验收)
- [项目结构](#项目结构)

---

## 环境与构建

- 需要 JDK（开发与验证使用 `openjdk 21.0.12.1`，见 `RUNLOG.md`）。无需 Maven/Gradle 或联网下载依赖。

```bash
./scripts/build.sh          # 编译到 build/classes 与 build/test-classes
./scripts/test.sh           # 运行全部自动化测试
./scripts/run-server.sh     # 启动服务（默认 127.0.0.1:8080）
```

也可以直接用 `javac` / `java`（命令见 `RUNLOG.md`）。

服务启动参数：

```bash
java -cp build/classes com.example.segmenter.Main \
  [--port 8080] [--data-dir data/corpora] [--unknown-cost 10.0] [--default dict_v1]
```

## 快速开始

```bash
./scripts/run-server.sh --port 8080
# 另一个终端
curl -s -X POST http://127.0.0.1:8080/segment \
  -H 'Content-Type: application/json; charset=utf-8' \
  -d '{"text":"研究生命的起源","n":1}'
```

返回（节选）：

```json
{
  "text": "研究生命的起源",
  "version": "dict_v1",
  "best": { "words": ["研究", "生命", "的", "起源"], "totalCost": 9.984499 }
}
```

作为库使用：

```java
Dictionary dict = CorpusLoader.load(Path.of("data/corpora/dict_v1.corpus"));
Segmenter seg = new Segmenter(dict);                 // 未知字代价默认 10.0
Segmentation best = seg.segment("研究生命");          // 1-best
List<Segmentation> top5 = seg.segmentNBest("研究生生命", 5); // N 最佳
// 切换版本 = 换用另一个不可变词典快照
Segmenter segV2 = new Segmenter(CorpusLoader.load(Path.of("data/corpora/dict_v2.corpus")));
```

## 算法说明

### 模型（DAG 最短路）

把原文按 Unicode 码点编号为位置 `0..n`。每条边 `i -> j` 对应一个候选词：

- **词典词边**：`text[i:j]` 在词典中，代价
  `cost(w) = -ln(freq(w) / F)`，`F` 为该版词典全部词频之和。高频词代价低。
- **未知字边**：在每个位置都无条件产生一条长度为 1 的边，对应一个未登录单字
  （生僻字、标点、空白、emoji 等都按单码点切出），代价为固定值
  `unknownCharCost`（默认 `10.0`，启动时可用 `--unknown-cost` 配置）。

于是“切分”就是从 0 到 n 的一条路径，路径代价为边代价之和；最小代价切分即
**最短路**，用 Viterbi 动态规划求解：

```
dp[i] = 到达位置 i 的最优假设
dp[0] = 空路径（代价 0）
dp[j] = 每个可能的前驱 i，取 min(dp[i] + cost(text[i:j]))
```

词典匹配用按码点组织的 **Trie**：从位置 `i` 行走原文，命中终止节点即松弛对应边。

### 同分确定性决胜（总顺序）

总代价按 `double` 精确比较；当两条路径代价相同（包括完全同分）时，按以下顺序逐级决胜，
保证结果**确定、与候选松弛顺序和词典插入顺序无关、可重复**：

1. **总代价**升序；
2. **词数**升序（同分倾向更少、更长的词）；
3. **词序列字典序**：从左到右逐词、按 Unicode 码点逐码点比较（前缀短者更小）；
4. 词面也完全相同时（某字既在词典中又可按未知字走），**未知标记序列**决胜：词典词 `false` 先于未知词 `true`。

比较沿假设链从右向左递归进行，不额外分配字符串。该顺序是一个**全序**（最后一级用未知标记兜底），
因此任意两条候选路径都可定序。

### N 最佳路径（多项式，非指数枚举）

`segmentNBest(text, n)` 使用 **list-Viterbi**：每个位置保留全序下前 `n` 条**互异**路径
（假设以链表形式存储前驱，不复制整段词序列）。由于该全序在“追加相同后缀”下保持一致，
在每个节点只保留前 n 条就足以得到全局前 n 条。

- 时间复杂度约 `O(n · L · N · log(LN))`，空间 `O(n · N)`（`L` 为最长词长，`N` 为请求条数，上限 64）。
- **不枚举** `2^(n-1)` 种切分。验证见测试 `PolynomialComplexityTest`：在每位置都高度歧义的
  合成文本上，1600 码点的 N=10 切分仍在亚秒级（实测数据见 `RUNLOG.md`）。
- 正确性以**穷举参考实现**对照：测试用的 `BruteForce` 枚举短句的全部切分，
  DP 的完整 N 最佳列表与穷举排序结果逐条一致。

### 词典与版本

- 语料文件是自建的“词 + 词频”列表（`data/corpora/*.corpus`，`#` 注释）。
- `Dictionary` 是**不可变快照**；“版本切换”即换用另一快照，线程安全。
- 内置两个版本用于演示版本切换对重叠词的影响：
  - `dict_v1`：`研究生` 词频极低、单字 `生` 高频 → “研究生生命”倾向 `研究 / 生 / 生命`；
  - `dict_v2`：`研究生` 高频且含 `命` → 倾向 `研究生 / 生命`。
  - 实测两版 1-best 不同，且分别与各自穷举最优一致（见 `RUNLOG.md`）。

## HTTP JSON 接口

仅监听 `127.0.0.1`。

### `POST /segment`

请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `text` | string | 是 | 待切分文本（按 Unicode 码点；上限 100000 码点）。空串合法。 |
| `n` | integer | 否 | N 最佳条数，`1..64`，默认 1。 |
| `version` | string | 否 | 词典版本，默认取服务启动时的默认版本（`dict_v1`）。 |

响应（`n=1` 时额外给 `best` 别名）：

```jsonc
{
  "text": "研究生生命",
  "codePoints": 5,
  "version": "dict_v1",
  "unknownCharCost": 10,
  "results": [
    {
      "rank": 1,
      "totalCost": 7.792473,
      "words": ["研究", "生", "生命"],
      "tokens": [
        {"word": "研究", "cost": 2.597491, "unknown": false},
        {"word": "生",  "cost": 2.597491, "unknown": false},
        {"word": "生命", "cost": 2.597491, "unknown": false}
      ],
      "tokenCount": 3,
      "unknownCount": 0
    }
    // ... 共 n 条（候选不足时更少），按总代价升序、互异
  ],
  "best": { /* n == 1 时 results[0] 的别名 */ }
}
```

### `GET /versions`

列出可用词典版本、默认版本、未知字代价、各版本词数与词频总和。

### `GET /healthz`

返回 `{"status":"ok"}`。

### 错误

| HTTP | `error` | 触发条件 |
|---|---|---|
| 400 | `invalid_json` | 请求体不是合法 JSON |
| 400 | `invalid_request` | 请求体不是 JSON 对象 |
| 400 | `missing_field` / `invalid_field` | 缺 `text`、字段类型错误、`n` 非 `1..64` 整数 |
| 404 | `unknown_version` | 指定的词典版本不存在（消息中列出可用版本） |
| 405 | `method_not_allowed` | 方法不对（如 GET /segment） |
| 413 | `text_too_long` | 超过 100000 码点 |

## 请求/响应样例

- 请求体：`examples/01-basic.json` … `examples/06-error-version.json`。
- 对应**真实抓取**的响应：`examples/responses/*.response.txt`（含 HTTP 状态码）。
- 一键复现：先启动服务，再运行 `./scripts/curl-examples.sh`。

覆盖：普通句、`dict_v1`/`dict_v2` 的 N 最佳与版本切换、含未知字符（希腊字母/中文标点）、
空串、未知版本报错。

## 测试与验收

自动化测试（`./scripts/test.sh`，自研极简断言框架，**183 个断言全部通过**，详见 `RUNLOG.md`）：

- `BasicSegmentationTest`：词典命中、未知字符（含 emoji 代理对按单码点处理）、空串、标点/空白、参数校验。
- `ExhaustiveComparisonTest`：**小句穷举全部切分**与 DP 的 1-best 和完整 N 最佳逐条对照，
  覆盖**重叠词、未知字符、空串、词典版本切换**（验收核心）。
- `TieBreakTest`：手工构造严格同分的歧义句，验证词数决胜、码点字典序决胜、
  词典词/未知词决胜，以及对词典插入顺序无关、重复运行稳定。
- `NBestTest`：排序、互异、rank 连续、数量上限、`n=1` 与 `segment()` 一致、与穷举前 n 条一致。
- `EmptyStringTest`：空串与单字词边界。
- `PolynomialComplexityTest`：高歧义长串上的运行时，确认随长度多项式增长、非指数爆炸；
  另含 4000 码点“深链同分”用例，强制字典序比较器沿整条长链工作（比较器为 O(深度) 迭代实现，不依赖调用栈深度）。
- `CorpusLoaderTest`：注释/空行、词频代价、重复词与坏行拒绝、快照不可变。
- `JsonTest`：解析/序列化往返、数字类型、转义、非法输入拒绝。
- `HttpServerTest`：在随机端口真实启动服务，用 JDK HttpClient 覆盖全部正常与错误分支。

## 项目结构

```
src/main/java/com/example/segmenter/
  Main.java                      服务入口与参数解析
  api/Segmenter.java             核心 DP（Viterbi + list-Viterbi N 最佳、决胜总序）
  model/Dictionary.java          不可变词典快照（freq -> -ln(freq/F) 代价）
  model/Trie.java                按 Unicode 码点的词典 Trie
  model/Token.java / Segmentation.java
  data/CorpusLoader.java         合成语料（词 词频）加载
  json/Json.java                 零依赖最小 JSON 解析/序列化
  service/SegmentServer.java     JDK HttpServer JSON 服务
src/test/java/com/example/segmenter/tests/
  AllTests.java                  测试入口
  TestFramework.java             极简断言框架
  BruteForce.java                穷举参考实现（仅测试用）
  *Test.java                     各验收测试
data/corpora/dict_v1.corpus, dict_v2.corpus   自建合成语料（两个版本）
examples/                        请求样例与真实响应
scripts/                         build.sh / test.sh / run-server.sh / curl-examples.sh
RUNLOG.md                        实际运行的命令、结果与说明
```
