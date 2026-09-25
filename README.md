# near-dup-clustering

文档近重复聚类的纯后端项目（Java 21）。本地计算，不调用任何外部搜索服务或大模型。
流水线：**词级 shingle → MinHash 签名 → LSH 分带候选召回 → 精确 Jaccard 复核 → 并查集连通分量聚类**。

## 构建 / 运行 / 测试

```bash
mvn test                                  # 32 个自动化测试
mvn package                               # 生成 target/near-dup-clustering-1.0.0.jar（含依赖）
java -jar target/near-dup-clustering-1.0.0.jar 8080   # 启动 JSON 服务（端口可用参数或 PORT 环境变量覆盖）
```

## 固定规则（可复现性契约）

| 项 | 值 | 位置 |
|---|---|---|
| 分词 | `toLowerCase(Locale.ROOT)` 后按 `[^a-z0-9]+` 切分，丢弃空 token | `Shingler` |
| shingle | 词级 k-shingle，k=3，token 以 U+0001 连接 | `Shingler` / `NearDupConfig.DEFAULT_SHINGLE_SIZE` |
| 短文档 | token 数 < k → shingle 集为空 → 相似度恒为 0，永不入簇（**有意为之的已知限制**） | `Shingler` / `Jaccard` |
| 哈希 | MurmurHash3 x64 128-bit，种子固定 42；第 i 个置换用双哈希 `h1 + i*h2` | `MurmurHash3` / `MinHash` |
| MinHash | 256 个签名值 | `NearDupConfig.DEFAULT_NUM_HASHES` |
| LSH | 64 带 × 4 行；任一带完全一致即成为候选 | `LshIndex` |
| 复核阈值 | 精确 Jaccard ≥ 0.5（请求可覆盖） | `NearDupConfig.DEFAULT_THRESHOLD` |
| 语料种子 | 20260922 | `CorpusGenerator.CORPUS_SEED` |

## 聚类语义：连通分量，不是团（clique）

簇 = 已验证边（Jaccard ≥ 阈值）构成的图上的**连通分量**。因此：

- 边 A–B、B–C 会把 A、C 放进同一簇，**即使 Jaccard(A, C) 低于阈值**；
- 结果中每个簇都带 `minPairSimilarity` / `maxPairSimilarity`（对簇内**所有**对精确计算）和
  `allPairsAboveThreshold` 标志 —— 我们**不会**把同簇的每一对都描述为超过阈值；
- `edges` 只列出真正通过复核（≥ 阈值）的文档对。

合成语料中的传递链（`chain-0` … `chain-4`，滑动窗口构造）实测：相邻对 0.659，
隔一个 0.417，端点对 0.097。5 篇文档通过连通性进入同一簇，但端点对远低于阈值，
该簇 `allPairsAboveThreshold = false`。

## 统计口径

`stats` 字段对每次运行给出候选召回与误报统计（ground truth 由暴力全对精确 Jaccard 得出）：

- `candidatePairs`：LSH 召回的候选对数
- `verifiedPairs`：候选中精确 Jaccard ≥ 阈值的对数（真阳性）
- `falsePositiveCandidates`：候选中被精确复核否决的对数（误报）
- `truePairs`：全体文档对中精确 Jaccard ≥ 阈值的对数
- `missedTruePairs`：LSH 漏掉的真对数（漏报）
- `candidateRecall = verifiedPairs / truePairs`，`candidatePrecision = verifiedPairs / candidatePairs`

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | `{"status":"ok"}` |
| GET | `/corpus/sample` | 返回内置合成语料（31 篇） |
| POST | `/cluster` | 请求体 `{"documents":[{"id","text"}...], "threshold"?}`，聚类自定义文档 |
| POST | `/cluster/sample` | 请求体 `{"threshold"?}`（可空），聚类内置语料 |

输入校验：文档列表非空（≤2000 篇）、id 非空且不重复、text 不为 null（≤200k 字符）、
threshold ∈ (0,1]；非法输入返回 400 与 `{"error": ...}`，错误方法返回 405。

请求样例见 [`samples/cluster-request.json`](samples/cluster-request.json)：

```bash
curl -s -X POST http://localhost:8080/cluster \
  -H 'Content-Type: application/json' \
  -d @samples/cluster-request.json | python3 -m json.tool
```

## 合成语料（`CorpusGenerator`，固定种子）

- 6 个主题 ×（1 篇基准 + 2 篇变异）：变异为 5% 替换 + 2% 删除，shingle Jaccard 实测 0.59–0.91；
- 5 篇传递链文档（9 个句子上的滑动 5 句窗口）；
- 短文档反例：两篇**完全相同**的 `"hello world"`（2 token < k=3 → 空 shingle 集 → 不入簇），
  一篇 3 词独立短文档；
- 5 篇词汇互不相交的干扰文档。

## 实测运行记录（2026-09-25，本机 OpenJDK 21.0.12 / Maven 3.8.7）

`mvn test`：**Tests run: 32, Failures: 0, Errors: 0, Skipped: 0 — BUILD SUCCESS**。

`POST /cluster/sample`（默认阈值 0.5）实测输出：

```json
"stats": {
  "documentCount": 31, "emptyShingleDocuments": 2,
  "candidatePairs": 25, "verifiedPairs": 19, "falsePositiveCandidates": 6,
  "truePairs": 19, "missedTruePairs": 0,
  "candidateRecall": 1.0, "candidatePrecision": 0.76
}
```

- 7 个簇（6 个主题簇 + 1 个传递链簇），8 个单例（5 干扰 + 3 短文档）；
- 传递链簇：5 个成员、仅 4 条相邻边（均 0.6585），`minPairSimilarity 0.0968`，
  `allPairsAboveThreshold false` —— 端点对未达阈值但仍同簇；
- 短文档 `short-a`/`short-b`（内容完全相同）未入任何簇，`emptyShingleDocuments = 2`；
- 6 个误报候选全部来自传递链非相邻对（0.417/0.236/0.097），被精确复核否决；
- 本轮 `candidateRecall = 1.0`（无漏报）。这是该固定语料与参数下的实测值，
  不是算法保证 —— MinHash/LSH 本质上是概率召回。

`POST /cluster`（`samples/cluster-request.json`）实测：`report-v1`/`report-v2`（0.7273）成簇；
`report-v3` 与 v1/v2 的 Jaccard ≈ 0.41 低于阈值，被 LSH 召回但被复核否决
（`falsePositiveCandidates = 2`），保持单例 —— 候选召回与精确复核的分工即在于此。

### 开发过程中实际遇到并修复的问题（如实记录）

1. **MurmurHash3 尾部处理 bug（已修复）**：初版把参考实现需要 case 贯穿（fall-through）的
   tail switch 写成了 Java 箭头 switch（不贯穿），导致短字符串只有最后一个字节参与哈希，
   大量 shingle 哈希碰撞。表现为 `MinHashTest` 中不相似集合估计值异常（1.0 / ≥0.1）。
   改为带贯穿的冒号 switch 后通过。
2. **语料变异率过高（已修复）**：初版 8% 替换 + 3% 删除使部分 base–variant 对
   Jaccard 跌至 0.44–0.46（低于 0.5 阈值），主题簇碎裂。降为 5% + 2% 后全部 ≥ 0.587。
3. **测试期望错误（已修复）**：`duplicateWindowsCollapseToSet` 初版期望 2 个 shingle，
   实际窗口 xyz/yzx/zxy 为 3 个不同 shingle，修正了测试期望。
4. 其余：Maven 默认 compiler 插件过旧不支持 `release 21`（固定 3.13.0 解决）；
   测试循环变量在 lambda 中引用需 effectively final（已改）。

## 已知限制

- token 数 < 3 的文档永远不参与聚类（空 shingle 集），即使两篇内容完全相同；
- 真值统计采用 O(n²) 暴力全对比较，故 API 限制单次 ≤ 2000 篇；
- LSH 候选召回是概率性的，阈值附近的真对可能漏召（本轮实测未发生）；
- 分词仅支持 ASCII 字母数字，非 ASCII 文本（如中文）会被切为空 token 流 —— 属有意简化的固定规则。

## 代码结构

```
src/main/java/com/example/neardup/
  MurmurHash3.java     128-bit 基础哈希（固定种子）
  Shingler.java        分词 + 词级 k-shingle（固定规则）
  MinHash.java         双哈希置换的 MinHash 签名
  LshIndex.java        分带候选召回
  Jaccard.java         精确 Jaccard 复核
  UnionFind.java       并查集（连通分量）
  Clusterer.java       流水线 + 统计 + 簇报告
  CorpusGenerator.java 固定种子合成语料
  NearDupConfig.java   不可变配置（默认值即固定契约）
  api/HttpService.java JDK 内置 HttpServer 的 JSON 服务
  Main.java            入口
src/test/java/...      32 个 JUnit 5 测试（单元 + 端到端 + HTTP）
samples/cluster-request.json
```
