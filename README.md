# 近似频繁项检测（Approximate Heavy Hitters）—— 纯后端实现

事件流上的**近似频繁项检测**库与 JSON 服务：Count-Min Sketch 点计数 + 有界候选集（bounded candidate set）做 Top-K，带翻滚时间窗口，支持兼容草图合并，时间与调度可注入，并提供小数据**精确参考实现**用于误差对照。

- 纯 Java（JDK 21，源码语法兼容 JDK 17+），**零第三方依赖**、无外部消息系统/数据库
- HTTP/JSON 服务基于 JDK 内置 `com.sun.net.httpserver.HttpServer`
- 构建只需要 `javac`（无 Maven/Gradle），脚本在仓库根目录

## 目录结构

```
src/approxheavy/
  core/        Clock / SystemClock / ManualClock，Scheduler / ExecutorScheduler / ManualScheduler
  hash/        Hashing（MurmurHash3 x64 128，可播种；Kirsch–Mitzenmacher 行哈希）
  cms/         CountMinSketch、IncompatibleSketchException、SketchJson
  candidates/  BoundedCandidates（容量有界的候选集）
  exact/       ExactCounter（精确 HashMap 计数，小数据参考实现）
  stream/      WindowSummary、EventStreamProcessor（翻滚窗口 + 分区合并）
  json/        Json（手写递归下降 JSON 解析/序列化）、JsonException
  server/      ApiServer、StreamRegistry、异常类型
  demo/        AccuracyDemo（验收用误差/碰撞/合并拒绝演示）
  Main.java    HTTP 服务入口
tests/approxheavy/tests/  6 个自动化测试套件（自带极简断言框架）
samples/                  请求样例 JSON 与 curl 走查脚本
build.sh test.sh run-demo.sh run-server.sh
```

## 快速开始

```bash
./build.sh                 # 仅 javac 编译到 build/classes
./test.sh                  # 编译并运行全部自动化测试
./run-demo.sh [随机种子]    # 验收演示：偏斜分布误差统计 / 构造碰撞 / 合并拒绝
./run-server.sh [端口]     # 启动 HTTP 服务，默认 8080
./samples/curl-walkthrough.sh http://localhost:8080   # 对运行中的服务走查全部接口
```

## 算法与语义（重要）

### Count-Min Sketch

- 参数：`width`（桶数）、`depth`（行数/独立哈希数）、`seed`（哈希族种子）。
- 点查询 `estimate(x) = min_j count[j][h_j(x)]` 是真实计数的**上界**：
  恒有 `estimate(x) ≥ trueCount(x)`，从不低估。
- 声明的误差上界（代码中 `errorUpperBound()`）：
  `ceil(2/(width-1) · totalCount)`，对任意单个键，过估计超过该界的概率为 `2^-depth`。
  本实现取 width 映射为 `⌈2/ε⌉`、depth 与 `ln(1/δ)` 对应，因此失败概率 `δ = 2^-depth`。
  也可用 `CountMinSketch.withError(ε, δ, seed)` 按标准 `width=⌈e/ε⌉, depth=⌈ln(1/δ)⌉` 构造。
- **只声明误差上界**：查询结果不声称覆盖谁是频繁项；是否是频繁项由候选集近似给出。

### 有界候选集 BoundedCandidates

- 最多保留 `capacity` 个键；满员时按"当前草图估计值最小、同级按进入顺序最早"淘汰。
- 草图是计数的唯一权威，候选集只决定"记住哪些键"。
- **不保证候选集合覆盖所有真实 Top-K**：真实头部键若在积累足够质量前被挤出，
  之后不再发生该键的事件时就永远不会重新进入。接口响应里显式带
  `candidateCoverageGuarantee: "none: a true top-K key may have been evicted"`。
- 测试与演示中用精确参考实现统计**召回率**（recall），但召回率是经验质量指标，不是保证。

### 合并兼容性

逐桶相加要求两侧哈希到完全相同的桶。以下情况**必须拒绝**（抛
`IncompatibleSketchException`，HTTP 为 409）：

- `seed` 不同：不同哈希族，键落入的桶毫无关系；
- `width` 不同：桶映射不同；
- `depth` 不同：行结构不同。

### 时间与调度注入

- `Clock`：生产用 `SystemClock`；测试用 `ManualClock` 任意拨时间。
- `Scheduler`：生产用 `ExecutorScheduler`（守护线程定时封窗）；测试用 `ManualScheduler` 手动触发。
- 事件可用显式 `timestampMillis`；未提供时取注入时钟的当前时间。
- 迟到事件（时间戳早于当前窗口）返回 `LATE`，不静默丢弃；跨边界自动补封（含空窗口）。

## HTTP/JSON 接口

| 方法 & 路径 | 说明 |
|---|---|
| `GET  /v1/health` | 存活检查，返回注入时钟的当前时间 |
| `PUT  /v1/streams/{name}` | 创建流（body 可配 width/depth/seed/candidateCapacity/windowMillis/retainedWindows） |
| `GET  /v1/streams` | 列出全部流及参数 |
| `POST /v1/streams/{name}/events` | 写入单条或批量事件；`timestampMillis` 可选，返回 accepted/late 计数 |
| `GET  /v1/streams/{name}/count?key=..&window=current\|last` | 点估计 + `errorUpperBound` + 保证说明 |
| `GET  /v1/streams/{name}/topk?k=..&window=current\|last` | 近似 Top-K，附**无覆盖保证**声明 |
| `POST /v1/streams/{name}/flush` | 立即封存当前窗口 |
| `GET  /v1/streams/{name}/windows` | 已封存窗口列表（含草图元数据） |
| `POST /v1/merge` | body `{"a": <sketch json>, "b": <sketch json>}`，兼容则返回逐桶之和，否则 409 |

状态码：200/201/202 成功，400 请求或 JSON 非法，404 流/窗口不存在，409 重复创建或草图不兼容。

草图 JSON 形态：

```json
{"type":"CountMinSketch","width":8,"depth":2,"seed":7,"totalCount":6,
 "cells":[[3,2,1,0,0,0,0,0],[1,1,3,1,0,0,0,0]]}
```

反序列化会校验行/列数与各行总和是否与 `totalCount` 一致，不一致直接拒绝。

## 实际运行记录（如实记录）

环境：`openjdk 21.0.12.1`（仓库所在机器无 Maven/Gradle，采用零依赖 javac 构建）。

### `./test.sh`（全部通过）

6 个套件共 **53 个用例全部 PASS**，端到端耗时约 **5.4 秒**（`real 0m5.385s`）：

- `JsonTest` 5/5：嵌套结构、转义、往返、畸形输入拒绝。
- `CountMinSketchTest` 16/16：不低估、声明界未被突破、同形合并相加、
  **异种子/异宽度/异深度合并均被拒绝**、JSON 往返与坏文档拒绝。
  其中一组 width=32/depth=5、10000 事件/300 键的随机流实测：
  `bound=646, maxObservedOverestimate=391`。
- `BoundedCandidatesTest` 7/7：容量、按最小估计淘汰、重复观测不扩容、
  排序、**构造"迟到热点"键被永久漏掉的场景以证明不保证覆盖**、候选合并裁剪。
- `WindowingTest` 9/9：时间戳归属、跨边界封窗、调度器定时封窗（含空窗口）、
  迟到拒绝、窗口保留上限、flush、同范围分区合并、**异种子分区合并被拒**、范围汇总。
- `ExactReferenceTest` 4/4：
  - 偏斜 Zipf（skew=1.1，50000 事件/2000 键，width=64 depth=5，候选容量 100）：
    `bound=1588 maxOver=1024 meanOver=243.441 breaches=0`，top-10 召回 `9/10=0.90`；
  - **构造碰撞**（width=2,depth=1 找到同桶键对）：两键估计都等于计数之和 547，
    过估计恰好等于对方质量，且不超声明界；
  - 增大几何尺寸（8×2 → 512×7）后最大过估计 2989 → 48；
  - 5 个不同种子重复实验，无一低估。
- `HttpServerTest` 12/12：对真实 JDK HTTP 服务发 HTTP 请求，覆盖全部路由、
  404/409/400 状态码、`/v1/merge` 对异种子与异宽度返回 409。

### 开发过程中实际出现过、并已修复的失败项

1. 候选集 `mergeWith` 容量已满时不做权重淘汰，仅"未满才加入"——用例
   `merge unions keys and respects capacity` 失败；改为与 `observe` 共用
   "按估计最小淘汰"逻辑后通过。
2. 窗口保留上限用例的期望值写错（时间推进实际密封 4 个窗口而非 3 个，
   最老保留窗口起点应为 20）——修正测试期望，代码行为正确。
3. "大草图零碰撞"断言过强（512×7 仍有最大 48 的过估计）——改为
   `大草图误差显著更小且 ≤100` 的统计性断言。
4. `ApiServer` 的线程池未 shutdown，测试 JVM 不退出导致命令超时——已在 `close()` 中关闭。

### `./run-demo.sh 42` 关键输出

- Zipf 偏斜流（skew=1.07，100000 事件/1000 键，width=64 depth=5，候选容量 50）：
  `declared errorUpperBound=3175 (3.175% of total)`，
  `observed max overestimate=1923, mean over distinct keys=445.559`，
  超过界的键 `0` 个。
- 近似 Top-10 中真实头部 item-8/item-9 被挤出，出现一个低质量假阳性 item-103，
  **召回率 8/10=0.80**——直接印证候选集不保证覆盖真实 Top-K（输出中显式标注
  `NO coverage is guaranteed`）。
- 强制碰撞（width=2, depth=1）：`item-0(1000)` 与 `item-1(37)` 同桶，
  两者估计均为 1037（分别过估 +37 / +1000），均未超声明界 2074；
  同一流在 width=512 depth=5 下估计精确（1000/37，界=5）。
- 合并：同 (width,depth,seed) 合并成功（total 14）；不同 seed / width / depth
  分别被拒并给出原因。

### HTTP 走查（`samples/curl-walkthrough.sh`，端口 19091 实测）

- 建流 → 批量写入 5 键（500/210/80/35/3）→ 单条补 12 次 page:home；
- `page:home` 点查询：`estimatedCount=512, totalCount=840, errorUpperBound=27`；
- `topk?k=3` 正确返回 home/product/cart，响应含 `candidateCoverageGuarantee:"none..."`；
- flush 后 `window=last` 查询结果一致；
- `/v1/merge`：兼容样例返回 totalCount=10 的合并草图（200）；
  异种子返回 `409 sketch seed mismatch: 99 != 7 ...`；
  异宽度返回 `409 sketch width mismatch: 16 != 8`。

## 局限与边界

- 单机内存库，未做持久化；重启后流状态丢失。
- 草图是 long 计数，极端总量下计数器可能溢出（未做饱和处理）。
- 误差界是经典 CMS 的单边概率界；需要严格确定性界可换 Count-Minimum-Mean 变体。
- 候选集召回率随容量与分布变化；容量远小于基数时真实头部可能缺失（接口明确不承诺覆盖）。
