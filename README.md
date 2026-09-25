# 词典分词动态规划（纯后端）

基于**词典代价**的中文最小代价分词库 + 本地 JSON HTTP 服务。纯 Java（JDK 21，**零第三方依赖**），
不调用任何外部搜索服务、不调用大模型；词典与输入语料均为本仓库内的**自建合成数据**。

## 功能

- **最小代价分词（Viterbi 动态规划）**：句子展开为切分格（DAG），每顶点只保留最优前驱。
- **未知字符有明确代价**：未登录单字按词典版本声明的 `@unknown-cost` 计价，逐字成边，
  响应里以 `known=false`、`dictCost=null` 标出。
- **同分确定性决胜**：总代价相同 → token 数少者优先 → 仍相同按 token 字面序列字典序
  （Unicode code point 序）决胜。任何输入都有唯一确定的最优切分。
- **N 最佳路径（不枚举指数候选）**：格上每个顶点只保留至多 K 条路径，
  复杂度 `O(n · L · K)`（L = 词典最大词长），不展开指数级切分。
- **穷举对照**：提供独立的 DFS 全量枚举分词器（限 16 字小句），与 DP 的前 K 条逐一对照。
- **词典版本切换**：目录下每个 `*.dict` 是一个版本，启动时一次加载，切换只换引用。
- **代价精确**：所有代价以放大 1e6 倍的 `long` 整数运算与比较，无浮点误差；支持 6 位小数。

## 目录结构

```
src/com/example/seg/
  model/Costs.java          代价：十进制字符串 <-> 放大整数
  model/Token.java          词/未知单字单元
  model/SegPath.java        完整路径 + 确定性决胜规则 tieCompare
  dict/Trie.java            词典前缀树（按起点枚举命中词）
  dict/Dictionary.java      词典版本（解析 .dict 文件 / 编程构造）
  dict/DictionaryRegistry.java  多版本注册表
  seg/Lattice.java          切分格：词典词边 + 未知单字边
  seg/DpSegmenter.java      Viterbi K 最佳（核心算法）
  seg/ExhaustiveSegmenter.java 小句 DFS 全量枚举（对照用，>16 字拒绝）
  service/Json.java         零依赖 JSON 解析/序列化
  service/SegService.java   业务编排（含参数校验与对照判定）
  service/SegHttpServer.java JDK HttpServer JSON 服务
  Main.java                 命令行入口（demo / seg / enum）
tests/com/example/seg/      54 个自动化测试（自带极简断言框架）
data/dicts/v1.dict,v2.dict  两个自建合成词典版本
data/corpus/sentences.txt   自建合成例句
samples/                    请求样例（*.json）与保存的真实响应（responses/）
scripts/                    build.sh / test.sh / run-server.sh
docs/RUN_LOG.md             实际运行命令与结果记录
```

## 构建与测试

需要 JDK（已在 OpenJDK 21 验证），无需 Maven/Gradle：

```bash
scripts/build.sh        # javac 编译 src 与 tests -> build/classes
scripts/test.sh         # 运行 4 个测试类共 54 个用例，失败返回非零码
```

## 命令行用法（不开服务也能验证）

```bash
java -cp build/classes com.example.seg.Main demo                 # 内置演示
java -cp build/classes com.example.seg.Main seg v1 研究生生命 5   # 前 5 条最佳
java -cp build/classes com.example.seg.Main enum v1 研究生生命    # 穷举所有切分
# 参数： seg <version> <text> [k] [dictDir]； enum <version> <text> [dictDir]
```

## JSON 服务

```bash
scripts/run-server.sh 8080            # 默认端口 8080，默认词典目录 data/dicts
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/dictionaries` | 词典版本列表及元信息 |
| POST | `/api/segment` | 分词；`k>1` 时附带 `nbest` |
| POST | `/api/crosscheck` | 小句（≤16 字）穷举 vs DP 对照 |

请求体：

```json
{ "dictionary": "v1", "text": "研究生生命", "k": 5 }
```

`/api/segment` 响应关键字段：`best.segmentation`（`/` 连接）、`best.totalCost`、
`best.tokens[]`（每个 token 含 `surface/known/cost/dictCost`），以及 `nbest[]`（带 `rank`）。

`/api/crosscheck` 响应含 `totalSegmentations`（穷举路径总数）、`match`（前 K 条是否逐一相同）、
`mismatches[]`、`exhaustiveTop[]`、`dpTop[]`。

错误响应统一为 `{"error": "CODE", "message": "..."}`：
`404 DICT_NOT_FOUND`、`400 MISSING_TEXT / TEXT_TOO_LONG / BAD_K / INVALID_JSON / ...`、
`405 METHOD_NOT_ALLOWED`。

请求样例与真实响应见 [`samples/`](samples)（请求体）和 [`samples/responses/`](samples/responses)，
也可直接运行 `samples/requests.sh`。

## 算法要点

切分格顶点 0..n，边表示占用 `[start,end)`：

- **词典词边**：从每个起点用 Trie 枚举所有命中的词，代价取词典声明值；
- **未知单字边**：每个未被长度 1 的词典词命中的字符单独成边，代价为版本未知字代价。

DP 在顶点 `v` 上保留至多 K 条到 `v` 的最佳路径，候选 = 入边接上起点保留路径；
按 `(总代价, token 数, 字面字典序)` 排序后截断到 K。空串是一条合法的 0 代价空路径。

穷举器与 DP **共用同一个 `Lattice`**，因此对照的差异只可能来自 K 最佳剪枝逻辑，
测试里对大量短句验证了两者前 K 条完全一致（含 300 个固定随机种子生成的句子）。

## 合成词典（版本切换示例）

句 `研究生生命`（研/究/生/生/命，5 字）：

| 版本 | 未知字代价 | 最优切分 | 总代价 |
|---|---|---|---|
| v1 | 5 | `研究/生/生命` | 3 |
| v2 | 8 | `研究生/生命` | 6.5 |

v1 中单字 `生=1` 便宜，v2 抬高 `研究/生命/生` 并保持 `研究生=2.5`，最优随之改变。

## 验收点对照

| 验收要求 | 对应实现 / 证据 |
|---|---|
| 小句穷举分词对照 | `/api/crosscheck`、`ExhaustiveSegmenter`；`SegTest` 多句对照 + 300 随机句 |
| 覆盖重叠词 | `结婚的和尚未结婚的`（结婚 / 尚未），见 `SegTest` 与 nbest 样例 |
| 未知字符 | `研究生命起源X`、连续未知 `YZ`，`known=false` 与未知代价均有断言 |
| 空串 | `SegTest.testEmptyString`、`segment-empty.json` |
| 字典版本切换 | v1/v2 同句最优不同，`DictTest`/`ServerTest` 与 `segment-version-v2.json` |
| N 最佳不枚举指数 | 每顶点保留 K 条，`O(nLK)`；k 上限 64，穷举器仅限 16 字对照 |
| 交付源码/README/请求样例/自动化测试 | 本仓库；运行记录见 `docs/RUN_LOG.md` |
| 实际运行并如实记录 | `docs/RUN_LOG.md`（命令、状态码、结果、过程中出现过的失败） |
| 不做前端 | 只有 CLI 与 JSON HTTP 接口，无任何前端资源 |
