# 连接顺序优化（Join Order Optimizer）——纯后端单机内存查询引擎

用 Java 从零实现的**单机内存等值连接查询引擎 + JSON 请求入口**，核心运算全部手写，
**不依赖任何 SQL 引擎 / 第三方库**（JSON 解析也是自写的极简实现）。

功能：

- 最多 **8 张表**的内连接（等值连接）顺序优化，**稠密（bushy）动态规划**，O(3ⁿ)；
- 代价基于**行数估计与各列不同值数（NDV）**：`|R ⋈ S| = |R|·|S| / max(NDV)`，多条件连乘；
- 自动识别**断开的连接图**（多个连通分量），分量之间以**笛卡尔积**连接，并在计划中
  逐节点标记 `cartesian=true`，同时给出告警；
- 同一份输入还会跑**左深（left-deep）DP**作对照，并支持 **暴力枚举全部合法二叉计划**
  验证 DP 拿到的就是全空间最小估计代价；
- 优化器产生计划后，**内存执行器真正执行**（Hash Join + 嵌套循环笛卡尔积），
  回填每个节点的**实际行数 / 实际 NDV / 实际代价**，直接对比估计偏差；
- 计划、DP 表、数据均可导出为 JSON 文件。无任何前端。

---

## 1. 目录结构

```
src/main/java/joinorder/
  Json.java            零依赖 JSON 解析/序列化
  ColumnRef.java       "表.列" 规范列引用
  Stats.java           行数 + 各列 NDV
  Table.java           内存表（数据行 + 提供统计 + 实际统计）
  EqPredicate/Edge     等值谓词与连接图的边
  Model.java           请求装载、校验、连通分量、边界列
  CostModel.java       基数估计（行数 + NDV）与代价口径
  PlanNode/...         ScanNode / JoinNode 计划树
  BoundarySig.java     子计划“边界签名”（候选去重，保证最优性的关键）
  Optimizer.java       稠密 bushy 动态规划（主优化器）
  LeftDeepOptimizer.java  左深 DP（对照基线）
  BruteForce.java      枚举全部合法二叉计划（正确性校验）
  Executor.java        内存 Hash Join / 笛卡尔积执行器
  Engine.java          门面：组装请求→优化→校验→执行→响应
  Main.java            CLI 入口
src/test/java/joinorder/
  AllTests.java        零框架自动化测试（1651 个断言）
samples/               5 个请求样例
outputs/               样例运行产物（响应/计划/DP 表）
build.sh test.sh run.sh
```

## 2. 构建与运行

需要 JDK 17+（开发与验证使用 OpenJDK 21），无外部依赖。

```bash
./build.sh            # 编译到 build/classes
./test.sh             # 运行全部自动化测试
./run.sh samples/01-basic-4tables.json out.json \
        --plan-out plan.json --dp-out dp.json
# 也可从标准输入：
cat samples/01-basic-4tables.json | java -cp build/classes joinorder.Main
```

退出码：0 成功；2 请求/执行错误（错误信息以 JSON 返回）。

## 3. 请求格式

```jsonc
{
  "name": "查询名（可选）",
  "strategy": "bushy",            // bushy（默认） | left-deep
  "statsSource": "provided",      // provided（默认，用表自带 stats）| actual（用真实数据统计）
  "execute": true,                // 是否实际执行计划
  "previewLimit": 10,             // 结果预览行数
  "verifyBruteForce": true,       // 是否枚举全部合法计划校验 DP
  "bruteForceMaxTrees": 200000,   // 枚举计划数上限（超过则跳过穷举，DP 照常）
  "tables": [
    {
      "name": "orders",
      "columns": ["id", "cust_id"],          // 可选（有 rows 时自动推导）
      "rows": [ { "id": 1, "cust_id": 1 } ], // 真实数据（执行必需）
      "stats": {                              // 提供给优化器的统计（可故意倾斜）
        "rowCount": 1000,
        "ndv": { "orders.cust_id": 100 }      // 键可用 "表.列" 或裸列名
      }
    }
  ],
  "joins": [
    { "on": [ { "left": "orders.cust_id", "right": "customers.id" } ] }
    // 一个 joins 条目 = 两表之间的一条边，可含多个等值条件（组合键）
  ]
}
```

约束与校验：表数 1..8（超过直接报错）；列引用必须 `表.列` 且表/列存在；
同一连接条目里的条件必须在相同两表之间；执行时所有表必须有 `rows`，
否则需 `execute=false` 只做代价估计。

## 4. 代价模型

基数估计（等值连接，各条件选择率独立）：

```
|R ⋈a=b S| = |R| · |S| / max(NDV(R.a), NDV(S.b))
```

多个等值条件时分母连乘；结果不超过笛卡尔积；两侧非空但估计 < 1 行时按教科书约定
取 1 行。输出列 NDV 传播为 `min(输入 NDV, 输出行数)`。未显式给 NDV 时退化为
“每行一个不同值”（`NDV = 行数`）。

代价口径（System-R 风格总处理元组数）：

```
cost(Scan t)        = |t|
cost(Join(A,B))     = cost(A) + cost(B) + |A ⋈ B|
```

优化器最小化根节点累计估计代价。

## 5. 动态规划与两个正确性要点

### 5.1 为什么候选不能只留一个（本项目踩过的坑）

朴素 DP 对每个表集合只保留“代价最小”的一个计划。但两个子计划即使累计代价相同，
**输出列的 NDV 不同**，放到上层连接里 `max(NDV)` 不同，会导致上层基数反转。
测试中曾出现 DP 比“全空间枚举最小值”差 2% 的反例（随机 6 表图 seed=93）。

解法：保留所有统计互不相同的候选。为避免候选爆炸，只比较**边界签名**——
边界列 = 出现在“跨越该表集合与外部表的连接边”上、属于集合一侧的列。
内部列的 NDV 在该子计划之上的任何连接中都不会被读取，签名相同的两个子计划
对上层完全等价，只留代价最小者。8 表最坏形状下每子问题候选 ≤ 9，优化 < 100ms。

### 5.2 笛卡尔积与断开的连接图

DP 枚举每个表集合的**全部**非空真二分：

- 二分**跨越连接边** → 等值连接（施加该边上的所有谓词）；
- 二分**不跨越任何边** → 笛卡尔积（`cartesian=true`）。

因为只有在“没有边被跨过”时才生成笛卡尔积，**等值谓词永远不会丢失**（被跨过的边
要么落在某侧子树内部，要么就是本次等值连接），即估计与执行都不改变连接语义。

连接图有多个连通分量时，分量之间没有边，其最终组合必然是笛卡尔积——响应里会给出
分量清单和告警。值得注意：即使在连通分量内部，最优计划也可能包含一次降低总代价的
小规模中间笛卡尔积（本项目 200 个随机图的穷举校验证明了必须允许这一点）。

### 5.3 暴力枚举校验

`BruteForce` 对 n≤6（及 8 表小计划数图）枚举**全部合法二叉计划**（8 表链图
251,881 个计划，约 60ms），使用与 DP 完全相同的边界签名去重，取全空间最小估计代价。
响应中 `bruteForceVerification.matchesMinimum` 为 `true` 表示 DP 代价等于穷举最小值。

## 6. 验收点与对应样例

| 验收要求 | 位置 / 命令 |
|---|---|
| 枚举所有合法顺序验证最小估计代价 | `samples/01`、`04` 响应的 `bruteForceVerification`；测试 `bruteForceMatchesDp*`（200 个随机 6 表连通图 + 断开图 + 8 表图） |
| 断开连接图、标记笛卡尔积、语义不变 | `samples/02-disconnected-cartesian.json`（3 个分量，计划中两个 `cartesian-join`，实际 18 行 = 3×2×3） |
| 倾斜统计：估计 vs 实际行数差异 | `samples/03-skew-stats.json`：估计 20 行、实际 400 行（**低估 20 倍**，数据聚集在单键）；`samples/05-skew-overestimate.json`：估计 20 行、实际 0 行（**高估**，键值域不相交） |
| 最多 8 表 | `samples/04-eight-tables.json`：8 表链，枚举 251,881 个计划，DP=穷举 |
| 稠密 vs 左深 | 每个响应的 `strategyComparison`；测试确认稠密永不劣于左深，并存在严格更优实例（n=6 随机图，代价差最大约 84,877） |
| 数据 / 计划可导出 | `--plan-out` / `--dp-out` / 响应文件；`outputs/` 下有本次运行产物 |

### 倾斜样例的实测数字（本次运行）

```
03-skew-stats（低估）：orders ⋈ customers  估计 20 行 / 实际 400 行（ratio 0.05）
05-skew-overestimate（零匹配高估）：       估计 20 行 / 实际 0 行
```

## 7. 自动化测试

`./test.sh` 运行零框架断言 runner，共 **1651 个断言**（本次全部通过），覆盖：

- JSON 解析/序列化往返与错误输入；
- 代价模型手算（单条件、组合键、笛卡尔积、最小值 1 行约定）；
- 连通分量识别；bushy/左深/暴力三者代价一致性；
- **200 个随机 6 表连通图**上 DP 代价 == 全空间穷举最小值；
- 8 表上限（9 表报错）、断开图、笛卡尔积标记、谓词不丢失的执行结果校验；
- Hash Join（一对多）、组合键连接、笛卡尔积、null 不参与等值连接；
- 倾斜统计低估/高估、`statsSource=actual` 时估计与实际一致；
- 对称统计下计划确定性可复现；端到端 JSON 文本链路。

## 8. 已知简化

- 内存执行器为教学规模设计：笛卡尔积按乘积物化大结果，无外连接/聚合/投影下推；
- 仅等值内连接（非等值条件不在连接图模型内）；
- 行数据建议使用规范列（输出时自动还原为裸列名）；
- 基数估计是经典“独立性 + 含值”模型，键值相关/不相交/倾斜正是其偏差来源，
  本项目通过 `samples/03`、`samples/05` 如实展示这类偏差，而非掩盖它。
