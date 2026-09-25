# 编辑距离候选筛选（纯后端）

一个零第三方依赖的 Java 本地文本检索/分析库 + JSON HTTP 服务：对自建合成语料做
**Unicode 码点 Levenshtein 距离**的阈值候选检索。粗筛索引保证**不遗漏**任何阈值内结果，
再用精确码点 Levenshtein 对候选逐个精算确认。不调用任何外部搜索服务或大模型，无前端。

## 1. 环境与构建

- JDK 11+（开发与实际验证使用 **OpenJDK 21**）；不需要 Maven/Gradle，不需要联网。
- Windows 下可直接执行 `javac`/`java` 命令（见下），仓库中的 `.sh` 脚本面向 bash。

```bash
./build.sh          # 仅编译主代码 -> build/classes
./test.sh           # 编译并运行全部自动化测试（失败退出码 1）
./run.sh [port]     # 启动 JSON 服务，默认端口 8080
java -cp build/classes com.example.edcand.App demo   # 控制台演示
java -cp build/classes com.example.edcand.App dist "a" "😀" NONE
```

手动等价命令：

```bash
mkdir -p build/classes && javac -encoding UTF-8 -d build/classes $(find src -name '*.java')
java -cp build/classes com.example.edcand.App serve 8080
```

## 2. 目录结构

```
src/com/example/edcand/
  CodePoints.java         码点展开（不使用 char/UTF-16 单元，不使用字节）
  TextNormalization.java  NONE/NFC/NFD/NFKC/NFKD + 大小写折叠
  Levenshtein.java        完整 DP 距离 + 阈值受限（saturating）精确距离
  CandidateIndex.java     长度门 + q=2 元组倒排候选索引（保证召回）
  CorpusGenerator.java    自建固定种子合成语料（含全部边界场景）
  SearchOutcome.java      结果与筛选统计
  Json.java               零依赖 JSON 解析/序列化
  SearchServer.java       JDK 内置 HttpServer 提供 JSON 服务
  App.java                CLI：serve / demo / dist
test/com/example/edcand/  零依赖测试框架与 22 个测试
samples/                  请求样例（curl --data-binary @文件）
RUNLOG.md                 实际运行命令与结果记录
```

## 3. HTTP 接口

仅监听 `127.0.0.1`，请求/响应均为 `application/json; charset=utf-8`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| GET | `/stats` | 词项数、q 值、规范化形式、距离单位 |
| POST | `/distance` | 两串的码点距离与码点长度 |
| POST | `/search` | 阈值候选筛选 + 精算，返回匹配与筛选统计 |

`/distance` 请求：`{"a":"...","b":"...","normalization":"NFC"}`（normalization 可省略，默认 NFC）。

`/search` 请求：`{"query":"...","threshold":2,"normalization":"NFC"}`。
响应字段：

```jsonc
{
  "query": "devolopment",
  "threshold": 2,
  "normalization": "NFC",
  "matches": [ {"term":"development","distance":1}, ... ],  // 已按距离、词项排序
  "totalTerms": 571,          // 语料规模
  "passedLengthGate": 39,     // |len(query)-len(term)| <= k 的词数
  "candidates": 2,            // 通过重叠下界、被送去精算的候选数
  "exactLevenshteinCalls": 2  // 实际执行码点 Levenshtein 的次数
}
```

错误请求（缺字段、坏 JSON、负阈值、非法枚举）返回 HTTP 400 与 `{"error":"..."}`。

快速体验：

```bash
./run.sh 8080 &
curl -s http://127.0.0.1:8080/stats
curl -s -X POST http://127.0.0.1:8080/search -H 'Content-Type: application/json' \
     --data-binary @samples/search-typo.json
```

## 4. 算法设计：粗筛不漏 + 精算确认

### 4.1 距离单位：Unicode 码点，绝不是字节

字符串先经 `codePointAt` 展开为码点序列再计算：

- emoji 😀（U+1F600，增补平面）在 UTF-16 中占 2 个 `char`、UTF-8 中占 **4 字节**，
  但码点数为 **1**，`distance("a","😀") = 1`（字节距离会是 4）。
- `é`(U+00E9) 与 `e` 的码点距离是 1，但二者 UTF-8 字节序列上的 Levenshtein 距离是 2，
  测试 `lev: codepoint distance must not equal UTF-8 byte distance` 同时计算两者并断言不等。

### 4.2 粗筛（两道必要条件，保证零漏报）

1. **长度门**：Levenshtein 距离 ≥ 长度差，故只检查 `|m-n| ≤ k` 的词项。
2. **q 元组多重集重叠下界（q=2，两端补哨兵）**。

长度 n 的码点序列两端各补 q-1 个哨兵后取相邻 q 元组多重集 G，|G(x)| = n+q-1。
若 `Levenshtein(x,y)=d`，一次编辑至多破坏 q 个 q 元组，因此

```
|G(x) ∩ G(y)|（多重集交，公共元组频次取 min） ≥ max(m,n)+q-1 − q·d
```

当 d ≤ k 时重叠度必然 ≥ `max(m,n)+q-1 − q·k`。**这是必要条件**：不满足的词一定不在
阈值内，满足的词交给精算。因此粗筛只可能多给候选（假阳性），不可能漏。
重叠计数基于二元组倒排索引，查询时只对命中过查询元组的词累加分数。

### 4.3 精算确认

对粗筛候选调用阈值受限（saturating）的码点 DP：真实距离 ≤ k 时返回**精确距离**，
超过 k 时提前以 k+1 截断并支持全行超阈早退。饱和截断不改变“是否在阈值内”的判定
（最优路径上的中间状态不超过最终距离），并有 6000 组随机对拍（含 emoji 码点池）
逐阈值与完整 DP 数值比对。最终只输出 d ≤ k 的词项，故输出无假阳性。

### 4.4 复杂度

- 建索引：对总码点数 N 为 O(N) 量级的 q 元组倒排；
- 查询：长度门 O(k 跨度桶) + 倒排累加（仅命中词）+ C 个候选各 O(n·k) 的阈值 DP。
  例：`devolopment` 在 571 词上，长度门剩 39，q 元组筛到 2，最终只做 2 次精算。

## 5. 规范化策略（组合字符如何处理）

- **默认 NFC + 大小写折叠**，且对索引和查询两侧统一执行。`café`（NFC, U+00E9）与
  `cafe + U+0301`（NFD）视觉等价但码点序列不同；NFC 后二者完全一致，距离为 0。
  测试覆盖：`samples/distance-nfd-nfc.json`、`recall: combining chars under NFC vs NFD`。
- 可在请求中切换：`NFD`（组合标记保留为独立码点）、`NFKC/NFKD`（兼容分解：
  全角 `ＡＢＣ→abc`、连字 `ﬁ→fi`、兼容数字 `Ⅲ→iii`）、`NONE`（原样码点比较）。
- 规范化只在码点层进行，不改变“按码点计数”的保证：emoji 在 NFC/NFD 下保持 1 码点。

**字素簇（grapheme cluster）说明**：码点是本项目明确选择的距离单位。像
`e + U+0301`、国旗 emoji（🇨🇳 = 两个区域指示符）、家庭 emoji（含 ZWJ 的多码点簇）
按“用户感知字符”计数会得到与码点不同的数字；需要字素簇语义时应在输入侧用
`java.text.BreakIterator.getCharacterInstance()` 分簇后再传入（README 记录该边界，
当前不默认分簇，以免悄悄改变距离定义）。

## 6. 语料

`CorpusGenerator`（固定种子 20260924，可复现）包含：空串、单码点串、NFC/NFD
组合字符、CJK、emoji 与区域指示符、全角字符/连字/兼容数字、空白变体、约 130 个
基础英文词及每词 3 个插入/删除/替换噪声变体，以及一个 13 成员的长公共前缀家族。
默认规模 571 个去重词项。

## 7. 自动化测试（22 个，全部通过）

- **随机小词表 vs 全扫描对拍（验收项）**：300 轮随机 ASCII 小词表 × 多查询 × 多阈值
  = **7200** 组，200 轮含 emoji/组合重音/CJK 的 Unicode 小词表 = **3000** 组；
  每组都与“全词表逐个完整 DP”比对：零漏报、零误报、距离数值一致、匹配数一致、
  筛选统计自洽。
- 空串查询/词项、组合字符 NFC/NFD、长公共前缀家族、k=0 精确匹配；
- 码点 vs UTF-8 字节距离对照；阈值受限 DP 与完整 DP 6000 组随机对拍；
- 规范化各形式行为；JSON 往返（含 emoji）；真实 HTTP 服务端到端（含 400 错误）。

运行方式与本次真实输出见 `RUNLOG.md`。

## 8. 设计边界

- 单机内存索引、JSON 短请求体；面向演示与验证，不做持久化、分片与鉴权。
- 距离单位为 Unicode 码点（非字素簇、非字节），已在接口 `/stats` 中显式标注。
