# 文档近重复聚类（MinHash 候选召回 + 精确 Jaccard 复核）

纯 Java 后端项目：本地文本分析库 + JSON 服务，**不依赖任何外部搜索服务或大模型**，
输入为代码内自建的**合成语料**（也支持 POST 自定义文本）。无前端。

- 语言/运行时：Java 21（仅用 JDK 自带 API，HTTP 用 `com.sun.net.httpserver`）
- 第三方依赖：**零**（无 Maven/Gradle、无外部 jar；测试框架为自带的轻量断言器）

---

## 1. 流水线与固定参数

```
文本
 └─ Shingler（固定分词 + k-shingle，k=3）
     └─ MinHash（120 个置换哈希，固定种子 SEED=20260924，仿射族 mod 2^61-1）
         └─ LSH banding（60 bands × 2 rows）= 候选对（召回阶段）
             └─ 对每个候选用 shingle 集合算精确 Jaccard 复核（阈值默认 0.6）
                 └─ 并查集 → 连通分量 = 聚类
```

所有参数都是硬编码/可在接口传入阈值的明确数值，`GET /health` 会回报：
`shingleK=3, minhashNumHashes=120, minhashSeed=20260924, lshBands=60, lshRows=2`。

### 分词与 shingle 规则（固定）

1. 英文先转小写；
2. token = 连续字母/数字串（英文按词），或**单个 CJK 汉字**（`U+4E00..U+9FFF`，中文按字）；
3. 以换行和句末标点（`. ! ? 。！？ ; ；`）切段，**shingle 窗口不跨段**；
4. 每段 token 数 ≥ k 时取所有连续 k-token 窗口（k=3）；不足 k 的段不产生 shingle；
5. shingle 用 64 位 FNV-1a 哈希，重复 shingle 去重（Jaccard 定义在集合上）。

推论：只有 2 个 token 的短文档 shingle 集合为空；**空集合不与任何文档相似**
（两个空集合的 Jaccard 也定义为 0，MinHash 空签名在 LSH 中不建桶）。

### MinHash（固定种子）

`h_i(x) = (a_i·x + b_i) mod p`，`p = 2^61−1`，`a_i∈[1,p)`、`b_i∈[0,p)`，
由 `new Random(20260924L)` 按固定顺序生成。种子是算法契约的一部分，不随输入变化。

### LSH（60 bands × 2 rows）

任一带内 2 个哈希值相等即成为候选。单带碰撞概率 `s²`，全带 S 曲线
`1−(1−s²)^60`：`s=0.2→≈0.92`，`s=0.33→≈0.999`，`s=0.6→≈1.0`。
刻意偏向**高召回、容忍误报候选**；误报由精确 Jaccard 复核剔除，两端指标都实测报告。

### 聚类语义（明确）

- 顶点 = 文档；边 = **精确** Jaccard ≥ 阈值的文档对；
- 聚类 = 该图的**连通分量**（并查集）。

因此：

- A–B、B–C 都过阈值而 A–C 不过时，`{A,B,C}` 仍是**同一个簇**（传递性）；
- **同簇不代表每对都超过阈值**。响应中 `clusters[].belowThresholdPairs`
  显式列出“同簇但该点对本身低于阈值”的所有点对；
- 短文档反例：文本相同但 shingle 为空（`s1`/`s2`）不合并；仅共享微小文档
  唯一 shingle 但并集 Jaccard 只有 0.2（`s3`/`s4`）也不合并（使用 Jaccard
  而非 containment，避免小分母误判）。

---

## 2. 合成语料（21 篇，全部由唯一 token 的合成句构造）

每个内容“单元”是一句恰好 3 个 token 的句子，且其 token 全局唯一，
所以每个单元恰好贡献 1 个唯一 shingle，shingle 集合可精确构造。

| 组 | 文档 | 设计意图 |
|---|---|---|
| ALPHA `a1..a8` | `a1` 为 13 单元核心；`a2` 为同集合换序；`a3..a7` 逐步替换；`a8` 漂移更远 | 7 文档近重复簇 + 1 个“看着像但不过阈值”的单例；`a1↔a7=8/18=0.444` 仍同簇 |
| CHAIN `c1,c2,c3` | 各 8 单元；相邻交集 6（并集 10）= **0.6**；`c1∩c3`=4/12=**0.333** | **传递链反例**：端点低于 0.5 却在同一簇 |
| BETA `b1,b2` | 交集 5、并集 7 ≈ 0.714 | 独立小近重复对 |
| ZH `z1,z2` | 各 4 个三字中文短句，共享 3 句 = **0.6** | 验证中文按字分词端到端可用 |
| SHORT `s1,s2` | 内容相同的两个汉字“你好”（<3 token → 空 shingle） | **短文档反例 1**：相同文本不合并 |
| SHORT `s3,s4` | 1 单元 vs 5 单元且共享该 1 单元，Jaccard=**0.2** | **短文档反例 2**：containment 会误判，Jaccard 不会 |
| `x1,x2` | 完全无关 | 单例 |

---

## 3. 实际运行结果（本环境真实执行）

环境：`openjdk 21.0.12`（Linux），无 Maven/Gradle。以下命令均已实际运行，
原始输出保存在 [`results/`](results/) 目录。

### 构建与自动化测试

```bash
bash scripts/build.sh      # javac 编译到 out/classes
bash scripts/test.sh       # 编译并运行全部测试
```

`results/test-output.txt` 结尾：

```
checks: 83, failures: 0
ALL TESTS PASSED
```

83 项断言覆盖：分词/边界/CJK/确定性、精确 Jaccard、MinHash 固定种子与估计误差、
LSH 在 0.6 相似度上的统计召回（300 对）与不相交集合零候选、连通分量/传递链、
短文档反例、阈值边界、自定义输入，以及 HTTP 端到端（含 400/405）。

### CLI 单次运行（阈值 0.6）

```bash
scripts/run.sh 0.6
```

`stats`（与 `results/sample-response-cluster.pretty.json` 一致）：

```json
{
  "documents": 21,
  "emptyShingleDocuments": 2,
  "bruteForcePairs": 210,
  "trueEdges": 20,
  "candidatePairs": 34,
  "candidatesAccepted": 20,
  "candidateFalsePositives": 14,
  "trueEdgesRecalled": 20,
  "trueEdgesMissed": 0,
  "candidateRecall": 1.0,
  "candidatePrecision": 0.58824,
  "falsePositiveShareOfCandidates": 0.41176
}
```

- **候选召回**：暴力枚举 210 对得到 20 条精确真边，LSH 全部召回（召回率 1.0，0 漏报）；
- **候选误报**：34 个候选中 14 个精确 Jaccard < 0.6，被复核剔除（误报占 41.2%）。
  其中包括刻意构造的 `c1-c3`（0.333）、`s3-s4`（0.2）以及 `a1..a8` 之间的漂移对；
- **聚类结果**：4 个多文档簇，大小 `{7, 3, 2, 2}`；单例
  `a8, s1, s2, s3, s4, x1, x2`。

`CHAIN` 簇在响应中的关键片段（证明端点不过阈值但同簇）：

```json
{
  "size": 3,
  "members": [{"id":"c1"},{"id":"c2"},{"id":"c3"}],
  "edges": [
    {"iId":"c1","jId":"c2","exactJaccard":0.6},
    {"iId":"c2","jId":"c3","exactJaccard":0.6}
  ],
  "belowThresholdPairs": [
    {"iId":"c1","jId":"c3","exactJaccard":0.33333,
     "note":"same cluster via transitive chain, but this pair itself is below threshold"}
  ]
}
```

ALPHA 簇（7 文档 21 对）也只有 16 条超阈值边，`belowThresholdPairs` 列出
`a1-a6=0.529`、`a1-a7=0.444`、`a2-a6=0.529`、`a2-a7=0.444`、`a3-a7=0.529`，
即**该簇内部并非每对都超过阈值**。

### 阈值 0.8 的对照（`results/cli-run-threshold0.8.json`）

真边只剩 7 条，候选仍为 34 个（误报 27，候选精度 0.206），0.6 构造的传递链
`c1-c2-c3` 不再合并（测试 `at threshold 0.8 the 0.6-chain does not merge`）；
ALPHA 仍靠 0.857 的相邻改写边连通。说明复核阈值只决定边，连通分量语义不变。

### HTTP 服务冒烟

```bash
scripts/serve.sh 8080          # 或 scripts/serve.sh 0 使用系统分配的空闲端口
bash scripts/smoke.sh          # 自动起服务 + curl 全路由（见 results/server-smoke.txt）
```

请求样例见 [`samples/REQUESTS.md`](samples/REQUESTS.md) 与
[`samples/*.json`](samples/)；真实响应见 `results/sample-response-*.json`
（`.pretty.json` 为格式化版）。

路由：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 固定参数与种子 |
| GET | `/corpus` | 内置合成语料（id/label/text） |
| POST | `/cluster` | `{}`、`{"threshold":0.6}` 或 `{"texts":[...]}`（可含 `{"text":...}` 对象） |

错误处理：非法 JSON → 400；方法错误 → 405；服务只绑定 `127.0.0.1`。

---

## 4. 目录结构

```
src/main/java/neardup/
  core/Shingler.java        固定分词 + k-shingle
  core/Jaccard.java         精确集合 Jaccard
  core/MinHash.java         固定种子的 120 维 MinHash
  core/Lsh.java             60×2 banding 候选召回
  core/UnionFind.java       并查集
  core/Clusterer.java       召回→复核→连通分量 + 召回/误报统计
  corpus/SyntheticCorpus.java  21 篇合成语料
  server/Json.java          零依赖 JSON 序列化/解析
  server/ApiService.java    结果 → JSON 结构（含 belowThresholdPairs）
  server/HttpApiServer.java JDK 内置 HTTP 服务
  Main.java                 serve [port] | run [threshold]
src/test/java/neardup/      6 个测试类 + 自带断言框架（83 项检查）
scripts/                    build.sh / test.sh / run.sh / serve.sh / smoke.sh
samples/                    请求样例与 curl 说明
results/                    上述命令的真实输出与样例响应
```

## 5. 已知限制与如实说明

- 合成语料刻意小（21 篇），便于人工核验每个点对；`Clusterer` 同时做 O(n²)
  暴力真相对照以统计召回率，生产中该对照应仅用于离线评测。
- LSH 参数偏向高召回（60×2），在 0.6 阈值下候选误报较高（41.2%），这是
  有意的取舍：候选阶段宁多勿漏，由精确复核保证正确性，两端数字均透明报告。
- MinHash 的 BigInteger 乘法实现正确但未做性能优化；语料规模下耗时可忽略。
- **未通过项**：无。构建、83 项自动化测试、CLI 与 HTTP 冒烟在本环境全部通过；
  早期开发中出现过的编译错误与 3 处断言期望值偏差（分词 bug、真实边数应为 20、
  自定义样例相似度不足）均已修复/订正并复测通过，过程以最终结果文件为准。
