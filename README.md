# edit-distance-filter — 编辑距离候选筛选服务

纯后端 Java 项目：本地文本检索库 + JSON HTTP 服务。不调用任何外部搜索服务或大模型，
语料为程序自建的合成语料。核心功能是：给定查询串和编辑距离阈值 k，从语料中找出
所有 Levenshtein 距离 ≤ k 的词条——**筛选阶段保证不漏（无假阴性），最后逐条精算确认**。

## 关键设计

### 1. 距离单位：Unicode 码点，不是字节、也不是 UTF-16 char

`Levenshtein` 先把字符串转成 `int[]` 码点序列再做动态规划：

- `é`（U+00E9）在 UTF-8 里是 2 字节，但距离按 **1 个码点**计；
- emoji `😀`（U+1F600）在 UTF-16 里是 2 个 char（代理对），但距离按 **1 个码点**计；
- 全项目不存在任何按字节或按 UTF-16 char 计算的距离。

### 2. 规范化策略（normalization）

- 默认 **NFC**：语料建索引时和查询进入时使用**同一个** `Normalize` 策略，
  因此 `café`（NFC，é 为单码点 U+00E9）与 `café`（NFD，e + 组合符 U+0301）
  规范化后完全相同，距离为 0。
- 可选 `NONE / NFC / NFD / NFKC / NFKD`（服务启动参数 `--normalize`），
  是**服务端级别**设置，保证索引侧与查询侧永远不会不一致。
- 规范化后相同的词条只索引一次（去重）。
- 已知边界：距离按码点计，不按字素簇（grapheme cluster）计——例如 `é` 视为
  e + ´ 两个码点（NFD 下），家庭 emoji 👨‍👩‍👧 是多个码点。如需按“用户感知的字符”
  计算，可在 `Levenshtein` 前加 `BreakIterator` 分段，本项目未启用。

### 3. 无损候选筛选（为什么不会漏）

`CandidateIndex` 对每个候选施加两个**必要条件**过滤——不满足条件的词条
一定距离 > k，因此过滤不会漏掉任何阈值内的结果：

1. **长度过滤**：`|len(s) − len(t)| ≤ k`（每次编辑最多改变长度 1）。
2. **q-gram 计数过滤**（q=2，两端各补 q−1 个哨兵符）：长度为 m 的串有
   m+q−1 个位置化 q-gram；一次编辑最多破坏 q 个，所以若 `dist(s,t) ≤ k`，
   两者 q-gram 多重集交集大小 ≥ `max(0, max(lenS,lenT) + q − 1 − k·q)`。
   交集用倒排索引（gram → 词条列表）按 `min(查询计数, 词条计数)` 精确累加。

### 4. 精算确认

通过筛选的候选逐条用 `Levenshtein.distanceWithin`（带行最小值提前退出的完整 DP）
计算精确距离，只保留 ≤ k 的结果。筛选去除的是“绝不可能”的词条，精算去除的是
“筛选放过但实际超阈值”的假阳性——**最终结果是精确的全集，与全扫描完全一致**。
这一点由性质测试保证：随机小词表 + 随机查询，索引结果与暴力全扫描逐条比对
（见 `CandidateIndexTest`）。

## 构建与测试

```bash
mvn test          # 运行全部自动化测试（20 个）
mvn package       # 产出 target/edit-distance-filter-1.0.0.jar
```

## 运行服务

```bash
java -jar target/edit-distance-filter-1.0.0.jar \
    --port 18099 --corpus-size 2000 --seed 42 --normalize NFC
```

参数均可省略，默认值即上面所示。

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | `/health` | 存活探针 |
| GET  | `/corpus` | 语料大小与规范化策略 |
| POST | `/corpus/reload` | 重新生成语料，body `{"size":2000,"seed":42}`（均可选） |
| POST | `/search` | 阈值搜索，body `{"query":"apple","k":2,"limit":20}`（k、limit 可选，默认 2、20；k ≤ 10，limit ≤ 1000） |

`/search` 响应：

```json
{
  "query": "apple",
  "normalizedQuery": "apple",
  "k": 2,
  "stats": {"corpusSize": 1995, "lengthPassed": 1088, "candidates": 62, "totalMatches": 47},
  "matches": [{"term": "apple", "distance": 0}, {"term": "apale", "distance": 1}]
}
```

`stats` 展示漏斗：语料总数 → 长度过滤通过 → q-gram 过滤通过（送精算的候选数）
→ 精算确认命中数。

## 请求样例

见 [examples/requests.sh](examples/requests.sh)，核心几条：

```bash
# 精确 + 模糊命中
curl -s -X POST http://localhost:18099/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"apple","k":2,"limit":10}'

# 组合字符：NFD 查询（é = e + U+0301）命中 NFC 语料词条，距离 0
curl -s -X POST http://localhost:18099/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"cafe\u0301","k":0}'

# emoji 按单个码点计
curl -s -X POST http://localhost:18099/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"😀","k":1}'

# 空串查询：k=0 时只命中空串词条
curl -s -X POST http://localhost:18099/search \
  -H 'Content-Type: application/json' \
  -d '{"query":"","k":0}'
```

## 合成语料

`CorpusGenerator` 用固定种子确定性生成，刻意混入验收要求的边角情形：
空串、NFC/NFD 两种写法的组合字符词（café/naïve/résumé/Zürich/ångström）、
40+ 字符长公共前缀词族、CJK 词、emoji 词，以及基础词的 1–3 次随机编辑变体。

## 测试覆盖（验收方式）

- `LevenshteinTest`：已知距离、空串、码点 vs 字节 vs UTF-16 的区分、
  组合字符随规范化策略的变化、长公共前缀、对称性、`distanceWithin` 与精确值一致。
- `CandidateIndexTest`（核心验收）：
  - 手工边角语料 × 3 种规范化策略 × 多个查询 × k=0..3，与全扫描逐一比对；
  - 30 组随机小词表 × 10 个随机查询 × k=0..3，与全扫描逐一比对；
  - 200 次“过滤器不得漏掉任何阈值内词条”的直接检查；
  - 1000 条生成语料的端到端比对。
- `JsonTest`：JSON 往返、`\uXXXX` 与代理对转义、畸形输入拒绝。
- `HttpApiTest`：真实起服务（临时端口）跑 HTTP 请求——搜索、Unicode/空串查询、
  参数校验（400/405）、语料重载。

## 项目结构

```
src/main/java/com/example/editdistance/
  Levenshtein.java       码点级 Levenshtein（精确 DP + 阈值提前退出）
  Normalize.java         规范化策略枚举
  CandidateIndex.java    无损候选索引（长度过滤 + q-gram 计数过滤，倒排索引）
  SearchService.java     查询流水线：规范化 → 筛选 → 精算 → 排序截断
  CorpusGenerator.java   合成语料生成器
  Json.java              零依赖极简 JSON 解析/序列化
  HttpApi.java           JDK 内置 HttpServer 的 JSON 服务
  Main.java              入口
src/test/java/com/example/editdistance/   4 个测试类，共 20 个测试
examples/requests.sh                      curl 请求样例
```

## 实测记录

见下文“运行记录”（命令与结果均为实际执行输出）：

- `mvn test` → `Tests run: 20, Failures: 0, Errors: 0, Skipped: 0`，BUILD SUCCESS。
- `java -jar ... --port 18099` 启动后，examples/requests.sh 中各 curl 均返回 200，
  响应字段与上文格式一致；`{"query":"café"(NFD),"k":0}` 命中 NFC 的 `café`，distance=0；
  `{"query":"","k":0}` 只命中空串；错误参数（缺 query、k=-1、k=99、非 JSON body、
  GET /search）分别返回 400/405。
- 开发过程中曾有 3 处**测试期望值**写错（flaw→lawn 实为 2、abc→aec 实为 1、
  NFC/NFD café 未规范化时距离实为 2），修正期望值后全部通过；实现代码未因此改动。
