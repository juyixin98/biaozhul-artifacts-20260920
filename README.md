# 滑动窗口精确分位数（Exact Sliding-Window Quantiles）

纯 Java 后端库 + JSON HTTP 服务：在**事件时间滑动窗口**上计算整数值事件的
**精确**中位数与任意分位数。不使用任何近似草图（t-digest / Q-Digest / HLL 等均无），
分位数结果与「把窗口内全部值取出来做全排序」完全一致。

- 纯 JDK，运行时零外部依赖（HTTP 用 `com.sun.net.httpserver`，JSON 自实现微型解析器）
- 时间与调度**可注入**：`Clock` 接口 + `SystemClock` / `ManualClock`，不依赖后台线程
- 过期事件按时间戳桶精确删除，同步扣减「值 → 计数」
- JDK 17+（开发与验证环境为 JDK 21）

## 目录结构

```
src/main/java/com/winquant/
  Clock.java                 时间源接口（可注入）
  SystemClock.java           系统墙钟实现
  ManualClock.java           手动推进时钟（测试/演示用）
  Quantiles.java             分位数定义 + 小数据全排序精确参考实现
  SlidingWindowQuantile.java 增量滑动窗口精确分位数（核心）
  json/Json.java             零依赖微型 JSON 解析/序列化
  server/Main.java           HTTP 服务入口
  server/QuantileServer.java JSON HTTP 服务
src/test/java/...            JUnit 5 测试（含随机化逐点对比验收测试）
examples/requests.sh         请求样例脚本
examples/sample-output.txt   实际运行输出存档
build.sh                     无 Maven/Gradle 时的构建+测试脚本（自动下载 JUnit 单 jar）
pom.xml                      可选：Maven 用户直接 mvn test
```

## 分位数定义（必须明确的规则）

设窗口内有 n 个值，升序为 x[0] ≤ x[1] ≤ … ≤ x[n−1]，分位点 q ∈ [0,1]：

1. **n = 0（窗口为空）**：分位数无定义。库返回 `OptionalDouble.empty()`，HTTP 响应中 `"value": null`。
2. **线性插值**（等价 numpy `np.quantile` 默认 "linear"、R type-7、Excel `PERCENTILE.INC`）：
   - h = q·(n−1)，i = ⌊h⌋，f = h − i
   - 结果 = x[i] + f·(x[i+1] − x[i])；i = n−1 时即 x[n−1]
   - 例：`[1,2,3,4]`，q=0.25 → 1.75；q=0.5（中位数）→ 2.5；q=0.75 → 3.25
3. **重复值规则**：每个事件独立计数，重复值按出现次数占用多个位次。
   `[5,5,5]` 任意分位数均为 5；中位数对偶数个值取中间两值的平均。
4. **整数值 → double 输出**：输入是 64 位整数，插值结果可能为半整数，故结果为 double；
   插值在 double 域计算，避免 `hi - lo` 的 long 溢出。

## 窗口语义

- 窗口为**左开右闭**区间 `(now − windowSize, now]`，`now` 来自注入的 `Clock`。
- 事件按**事件时间戳**（不是到达时间）入窗。`ts ≤ now − windowSize` 的事件过期删除，
  删除时从计数结构中扣减对应次数，计数为 0 的键移除；计数一致性由
  `RandomizedComparisonTest` 在 24 000 步随机流上逐步校验。
- 窗口边界临界规则：`ts = now − windowSize` 已在窗外（过期）；`ts = now` 在窗内。
- 迟到事件（到达时已过期）被拒绝并计入 `lateEvents`；乱序但在窗内的事件正常接受。
- 未来事件（`ts > now`）暂存，时钟推进越过其时间戳后自动入窗——这使时钟与事件到达
  完全可注入、可重放。
- 时钟单调；回退抛异常。
- 维护是**惰性**的：每次 add / 查询 / `refresh()` 时统一推进，不需要后台线程，
  因此“调度”由调用方决定（定时器、流水线水位线、测试手动推进皆可）。

## 数据结构与精确性

- `TreeMap<Long, Map<Long,Integer>>`：时间戳桶 → (值 → 次数)，用于按时间过期
- `TreeMap<Long, Long>`：当前窗口内 值 → 精确计数；分位数通过顺序扫描求精确秩 k 的顺序统计量，
  再按上面的插值公式计算
- 单次查询最坏 O(窗口内不同值个数)；插入/过期均摊 O(log 时间桶数 + log 不同值数)。
  这是“小数据精确参考实现 + 可扩展的精确计数实现”，**不是**近似结构。

## 构建与测试

### 方式一：无需 Maven/Gradle（已在本机实际验证）

```bash
./build.sh        # javac 编译；首次自动下载 junit standalone（约 2.8MB）到 lib/
```

### 方式二：Maven

```bash
mvn test
```

## 运行服务

```bash
# manual clock：时间通过 POST /advance 推进，适合演示/联调/测试
./run-server.sh                       # 默认 127.0.0.1:8080，窗口 100，初始时间 100

# system clock：跟随机器墙钟
java -cp build/classes com.winquant.server.Main --port 8080 --window 60000 --clock system
```

参数：`--port`（默认 8080，传 0 为随机端口）、`--window`（窗口长度，与时间戳同单位）、
`--clock system|manual`、`--start`（manual 时钟初值）。

## HTTP API

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| `POST /events` | `{"timestamp":100,"value":5}` 或 `{"events":[{...},{...}]}` | `{"accepted":n,"late":m}` |
| `GET /quantile?q=0.5` | — | `{"q":0.5,"count":k,"value":x}`；空窗口 `value:null` |
| `GET /median` | — | 同上，q 固定 0.5 |
| `GET /stats` | — | `{"now":...,"windowStart":...,"count":...,"lateEvents":...}` |
| `POST /advance` | `{"now":196}`（仅 manual clock） | `{"now":196}` |
| `GET /health` | — | `{"status":"ok"}` |

试跑：

```bash
./run-server.sh            # 终端 A
./examples/requests.sh     # 终端 B；实际输出见 examples/sample-output.txt
```

## 验收对照

| 要求 | 落实位置 |
|---|---|
| 与每窗全排序比较 | `RandomizedComparisonTest`：6 场景 × 4000 步，逐步对 count 与 7 个分位点做全排序对比 |
| 负值 | 随机场景“仅负值”、`QuantilesTest.negativeValues`、`SlidingWindowQuantileTest.negativeValues`、HTTP 样例 |
| 全重复 | 随机场景“全重复”、`allDuplicateValues`、`sameTimestampEventsAllCounted` |
| 同时间戳 | 随机场景“同时间戳突发”、`sameTimestampEventsAllCounted` |
| 窗口为空 | `emptyWindowHasNoQuantile`、`windowEmptiesAgainAfterAllEventsExpire`、随机流中反复出现空窗 |
| 过期删除正确维护计数 | `expiryRemovesCountsCorrectly` + 随机流每一步 count/late 计数对账 |
| 禁止近似草图 | 全部统计基于精确次数 `TreeMap`，无任何概率结构 |
| 时间/调度可注入 | `Clock` / `ManualClock` / `SystemClock`；惰性刷新无后台线程；`/advance` |
| 小数据精确参考实现 | `Quantiles.referenceQuantile`（复制+全排序） |

## 未通过项 / 已知限制

无。全部 29 个测试通过（实际运行记录见下）。已知范围限制：

- 单实例非水平扩展；线程模型为固定 4 线程 + 服务层加锁。
- 精确顺序统计量查询为 O(不同值个数)；若未来需要超大窗口，可在保持精确性的前提下
  改为桶式 Fenwick 树压缩值域，但本项目刻意不引入近似。
- JSON 解析器仅覆盖服务所需子集（对象/数组/字符串/数字/true/false/null）。
