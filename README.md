# Join Order Cost Planner（连接顺序代价规划服务）

纯 Java 后端服务：对**最多 8 张表的内连接**，用**动态规划**枚举连接计划，
选择估计代价最小的顺序，并输出**可解释**的计划（每条选择率从哪来、每个节点
估计多少行、代价如何累加）。同时提供**模拟器接口**：按 Zipf 倾斜分布合成数据、
**真实执行**哈希连接，比较「估计最优」与「真实最优」。

- 零第三方依赖：仅 JDK 17 标准库（HTTP 用 JDK 自带 `com.sun.net.httpserver`，
  JSON 为手写解析/序列化）
- 纯后端、无界面：HTTP + JSON
- 自动测试：130 个断言，全部通过（见 `results/test-run.log`）

---

## 1. 目录结构

```
src/joinplanner/
  model/      领域模型（表、边、计划树、选择率来源…）
  json/       手写 JSON 解析器 / 序列化器
  core/       校验、选择率解析、动态规划、左深穷举、计划响应组装
  sim/        Zipf 数据生成、真实哈希连接执行、估计 vs 真实对比
  web/        JDK HttpServer、路由
  test/       自动化测试（自带微型断言框架，无测试库依赖）
  Main.java   入口
samples/      请求样例
tools/        build.sh / run-tests.sh / run-server.sh / examples.sh
results/      一次真实运行的测试日志与接口输出
dependencies.lock   依赖锁定（唯一依赖：JDK 17 工具链）
```

## 2. 环境与依赖

- JDK 17 或更高（开发验证使用 Eclipse Temurin 17.0.12+7，x64 Linux，
  下载地址与 sha256 见 `dependencies.lock`）
- 无 Maven / Gradle / 任何第三方 jar
- 本机如无 `java`，解压 Temurin 压缩包后用 `JAVA_HOME` 指向它即可，例如：
  ```bash
  export JAVA_HOME=$HOME/opt/jdk-17.0.12+7
  ```

## 3. 构建、测试、启动

```bash
# 编译（输出到 classes/）
JAVA_HOME=/path/to/jdk17 tools/build.sh

# 运行全部自动化测试（进程退出码 0 即全部通过）
JAVA_HOME=/path/to/jdk17 tools/run-tests.sh

# 启动 HTTP 服务（默认 8080；可带端口参数）
JAVA_HOME=/path/to/jdk17 tools/run-server.sh 8080

# 对已运行的服务批量发送 samples/ 下全部请求样例
tools/examples.sh localhost:8080
```

也可直接使用 javac/java：

```bash
javac -d classes $(find src -name '*.java')
java -cp classes joinplanner.Main 8080
java -cp classes joinplanner.test.AllTests
```

## 4. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/plan` | 连接顺序规划 + 可解释计划 |
| POST | `/api/simulate` | 合成倾斜数据，真实执行，对比估计与真实代价 |
| GET  | `/health` | 健康检查 |

### 4.1 `POST /api/plan`

请求：

```json
{
  "tables": [
    {"name": "orders",    "rows": 100000},
    {"name": "customers", "rows": 10000, "unique": true},
    {"name": "lineitems", "rows": 500000}
  ],
  "edges": [
    {"leftTable": "orders", "leftColumn": "cust_id",
     "rightTable": "customers", "rightColumn": "id",
     "selectivity": 0.0001},
    {"leftTable": "lineitems", "leftColumn": "order_id",
     "rightTable": "orders", "rightColumn": "id",
     "ndvLeft": 100000, "ndvRight": 100000}
  ],
  "defaultSelectivity": null,
  "allowCrossProducts": false
}
```

字段说明：

- `tables[].rows`：基数，非负有限数。
- `tables[].unique`：声明键唯一（仅作元信息展示；缺统计时是否唯一会影响选择率
  缺省假设，见下）。
- 一条边 = 一个等值连接谓词 `leftTable.leftColumn = rightTable.rightColumn`。
  同一对表只允许一条边（复合条件请合并成一条边并直接给 `selectivity`）。
- 选择率解析顺序（响应 `edges[].selectivitySource` 可查）：
  1. 显式 `selectivity`（必须 `0 < s ≤ 1`）→ `GIVEN`；
  2. 由 `ndvLeft/ndvRight` 推导 `1/max(ndvL, ndvR)`：
     - 两侧都给 → `DERIVED`；只给一侧 → 另一侧假设唯一
       （ndv=该表基数）→ `ASSUMED_UNIQUE_OTHER_SIDE`，并写入 `warnings`；
  3. 都没给：若配置了全局 `defaultSelectivity` 则使用它 → `DEFAULT_FALLBACK`；
     否则两侧都假设唯一，`1/max(cardL, cardR)` → `ASSUMED_UNIQUE`，并写入
     `warnings`。
- `allowCrossProducts: true` 时允许断连图用笛卡尔积（选择率 1）拼计划；
  默认 `false`，断连图返回 **422** 并列出连通分量。

响应包含：

- `summary`：表/边数、总代价、`costOverflow` 标志、所用模型说明；
- `warnings`：统计缺失假设等非致命提示；
- `edges`：每条边的选择率、来源（`GIVEN/DERIVED/ASSUMED_*/DEFAULT_FALLBACK`）、
  推导文字；
- `plan`：二叉计划树，每个节点有 `type`(SCAN/INNER_JOIN/CROSS_JOIN)、
  `estimatedRows`、`nodeCost`、谓词与自然语言 `explanation`；
- `joinOrder`：计划树叶节点顺序（bushy 计划的叶子序列）；
- `enumeration`：**全部左深合法排列**及其估计代价（最多列前 100 个），
  供人工/测试核对；DP 本身搜索的是完整 bushy 空间。

错误码：400（请求结构/取值非法、JSON 损坏）、405（方法错误）、
422（`DISCONNECTED_GRAPH`，附 `components` 与修复建议）。

### 4.2 `POST /api/simulate`

在真实但可控的数据上检验估计器：

```json
{
  "seed": 99,
  "maxRows": 5000000,
  "problem": { "...": "与 /api/plan 请求体完全相同" },
  "columns": [
    {"table": "t0", "column": "k01", "ndv": 15, "zipf": 1.4}
  ]
}
```

- `problem` 也可以直接内联在顶层；
- 每个被边引用的列可用 `ndv`（不同值个数）与 `zipf`（Zipf 指数，0=均匀）
  描述；未列出的列默认 `ndv = 表行数`（唯一）、均匀分布；
- `seed` 决定确定性结果；
- `maxRows` 是安全闸：任一中间结果真实行数超过它即返回 **422
  SIMULATION_TOO_LARGE**（防止倾斜数据爆炸）。

服务端会：生成数据 → 对每个表子集用**多列分组聚合的哈希连接**真实计算精确
中间行数 → 枚举全部合法左深顺序，分别按「估计基数」和「实测基数」计分排名。
响应含每个子集的 `estimatedRows/actualRows/estimateOverActual`、每个顺序的
两种代价与排名，以及 `verdict`：`AGREE` 或 `DIVERGE`（分歧时给出估计最优顺序
的真实代价、真实最优代价与 `regretRatio`）。

> 注意：模拟器表行数按整数处理；真实代价是**左深顺序**的中间行数之和，
> 这与「左深最优」一一对应。`/api/plan` 的 DP 搜索 bushy 空间，两个接口的
> 最优概念差异在响应字段注释中均有说明。

## 5. 算法与代价模型

### 基数模型（经典教科书乘积模型）

对表子集 S：

```
|S| = Π |T|  ×  Π selectivity(e)        （T ∈ S；e 为两端都在 S 中的边）
```

- 基数乘法在 **log 域**完成，避免 8 表连乘直接溢出；代价求和用数值稳定的
  logAdd。最终超过约 `1e300` 行（或变成 Infinity）时，代价仍返回完整计划，
  但对应数值置 `null` 并设 `costOverflow / rowsOverflow` 标志。
- 该模型假设**每条边选择率恒定且跨边独立**——这正是倾斜数据下估计失真的来源，
  `/api/simulate` 专门暴露这一点。
- 基数为 0 的基表使结果为 0（log 中 −∞ 正确传播）。

### 动态规划（bushy 空间，O(3^n)）

对每个表子集掩码 S 保留构造 S 的最优二叉树：枚举 S 的所有二分 (A,B)，
当有边跨越 A–B（内连接）时合法；`allowCrossProducts` 时也允许把两个各自连通
的分量做笛卡尔积。

```
cost(S) = min_{A∪B=S, 有跨边} cost(A) + cost(B) + |S|
cost(单表) = 0
```

- 内部不连通的子集不可实现（DP 中标记）；全集不可实现时报断连图错误。
- 平局用确定性规则（较小的左侧掩码）打破，相同请求永远得到相同计划。

### 校验与异常情况

- 断连图：并查集求连通分量，默认 422 + 分量列表；可选笛卡尔积。
- 统计缺失：按上面的选择率解析链处理，每次假设都进 `warnings` 并标注来源。
- 代价溢出：log 域计算 + 溢出标志 + `null` 数值（JSON 本身无法表达 Infinity）。
- 非法输入：表数 1–8、行数有限非负、选择率 `(0,1]`、名称唯一、不允许自连接/
  重复边/悬空引用，违反一律 400 并给出 JSON 路径。

## 6. 验收点如何落实

1. **小规模枚举全部合法顺序核对最优估计代价**
   - `PlannerTest` 对 n=1..6 的 48 个确定性随机连通图（含环边），用独立的
     递归穷举枚举**所有二叉 bushy 计划**，与 DP 最优逐一相等；
   - 另有手算用例（2 表、3 表链、4 表星）核对绝对代价；n=8 边界用例验证
     DP 不劣于最好左深顺序。
2. **倾斜数据比较估计与实际中间行数、区分估计最优与真实最优**
   - `SkewTest` 用手工小数据核对执行器连接结果；
   - 多个 seed 的 Zipf 4 表链上断言：至少一个子集的估计/实测偏差 >50%；
     全部 8 个合法链顺序按两种模型计分；且至少一个 seed 出现
     `estimated optimum ≠ true optimum`（seed 99 时估计第 1 的顺序真实排第 5）。
3. **异常**：`ValidationTest`（断连 422/分量/笛卡尔积、13 类非法输入）、
   `EdgeCaseTest`（0 行表、1e600 溢出、单表）。
4. **HTTP**：`HttpTest` 真实启动临时端口服务，覆盖 200/400/405/422 与
   simulate 全链路。

## 7. 实测运行结果（本次交付机器）

环境：Ubuntu 24.04，Temurin JDK 17.0.12+7（无 root，解压到 `~/opt` 使用）。

```
TOTAL 130, PASS 130, FAIL 0          # 完整日志见 results/test-run.log
```

对 `samples/` 真实发请求（输出原文保存在 `results/*.out.json`）：

- `plan_basic` → 200，总代价 600000，计划先做 orders⨝customers（10 万行）
  再连 lineitems；
- `plan_disconnected` → 422，返回两个分量与 remedy；
- `plan_cross` → 200，CROSS_JOIN 拼计划，最终 5000 行；
- `plan_overflow` → 200，`costOverflow=true`，溢出数值为 null，计划完整；
- `sim_skew`（seed 99）→ `verdict=DIVERGE`：
  - 估计最优顺序 `[t2,t3,t1,t0]`（估计代价 208），真实代价 89562；
  - 真实最优顺序 `[t0,t1,t2,t3]`（真实代价 87619），regretRatio≈1.022；
  - 最终 4 表连接估计 48 行、实测 77149 行（低估约 1600 倍）。

## 8. 已知限制 / 未完成项

- 基数模型是「边选择率恒定 + 跨边独立」的乘积模型，不维护直方图/相关系数；
  倾斜与相关列导致的失真由 `/api/simulate` 显式暴露，但规划器本身不会自校正。
- 同一对表只支持一条边；不支持外连接、自连接、非等值谓词与投影/聚合代价。
- 模拟器为保证精确性会物化（分组后的）中间结果，行数受 `maxRows` 保护；
  它用于教学/验证规模（几百~几千行），不是大数据执行引擎。
- `/api/simulate` 的「真实最优」在左深顺序空间内枚举（n! 次排列，n≤8 即
  最多 40320），没有枚举真实 bushy 最优；估计侧 DP 仍是完整 bushy 空间。
- 无鉴权/TLS/持久化，仅适合本地或受信网络演示。
