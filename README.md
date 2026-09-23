# 连接顺序代价规划服务（Join Order Cost Planner）

纯 Java（**JDK 17，零第三方依赖**）实现的多表内连接顺序规划器：给定各表基数、
连接键的唯一性 / NDV / 选择率和连接边，用**动态规划**在所有（含 bushy 的）二元连接树中
选出估计总代价最小的顺序；同时提供数据生成 + 真实哈希连接执行器，用来对比**估计最优**与
**真实最优**。仅后端，通过 HTTP JSON 接口使用。

- 最多 **8** 张表
- 显式处理：**断连图**（拒绝静默笛卡尔积）、**缺失统计信息**（默认选择率 + 显式告警）、
  **代价/基数溢出**（饱和到上限并标记，绝不产生 NaN/Infinity）
- 输出**可解释计划**：树形计划、逐步执行、线性表达式、每个子集的估计基数、每条边选择率的来源

---

## 1. 目录结构

```
.
├── src/joinplanner/
│   ├── Main.java                 # 入口：java joinplanner.Main [port]
│   ├── json/                     # 自研 JSON 解析/序列化/类型校验（无第三方库）
│   ├── model/                    # Table/Edge/Spec、请求解析与校验
│   ├── plan/                     # Estimator、Planner(DP)、BruteForce(穷举)、连通分量、渲染
│   ├── sim/                      # 倾斜数据生成、真实哈希连接、真实代价 DP
│   └── http/ApiServer.java       # JDK com.sun.net.httpserver 路由
├── test/joinplanner/             # 自研零依赖测试（6 个测试类，1300+ 断言）
├── samples/*.json                # 请求样例
├── samples/results/*.json        # 对真实运行服务跑出的响应（已存档）
├── scripts/
│   ├── build.sh                  # javac 编译（主代码 + 测试）
│   ├── test.sh                   # 运行全部测试
│   ├── run.sh                    # 启动服务
│   └── run_samples.sh            # 对全部样例发请求并存档
├── DEPENDENCIES.md               # 依赖与工具链锁定说明
└── README.md
```

## 2. 依赖与启动

**唯一依赖：JDK 17+**（开发与验证用 OpenJDK 17.0.20.1）。无需 Maven/Gradle，
构建期与运行期都不访问网络。详见 [`DEPENDENCIES.md`](DEPENDENCIES.md)。

```bash
# 编译（产物在 out/）
./scripts/build.sh

# 运行自动化测试
./scripts/test.sh

# 启动 HTTP 服务（默认 8080，可传端口）
./scripts/run.sh 8080
# 等价于：java -cp out joinplanner.Main 8080
```

启动后：

```
Join order planner listening on http://localhost:8080
Endpoints: GET /health | POST /plan | POST /enumerate | POST /simulate
```

## 3. HTTP 接口

| 方法 | 路径 | 作用 |
|------|------|------|
| GET  | `/health` | 健康检查 |
| POST | `/plan` | DP 求最优连接计划（bushy 全空间，或仅左深） |
| POST | `/enumerate` | 枚举**全部合法左深顺序**并独立穷举 bushy 最优，用于交叉核对 DP |
| POST | `/simulate` | 按声明的分布生成数据、**真实执行哈希连接**，用实测基数再规划，对比估计/真实最优 |

状态码：`200` 成功；`400` JSON 非法或参数不合法；`405` 方法不对；
`422` 连接图断连（响应体给出每个连通分量的计划）；`500` 内部错误。

### 3.1 `POST /plan`

请求字段：

- `tables[]`：`name`、`rows`（基数，≥0，≤1e18）；1..8 个，名字唯一。
- `edges[]`：`left`、`right`（表名，不允许自连接）、可选 `on`（谓词文字）。
  选择率按以下优先级确定（**边选择率的来源会在响应里逐条说明**）：
  1. 显式 `selectivity` ∈ [0,1]；
  2. 双边 `leftNdv`/`rightNdv`：`sel = 1/max(ndvL, ndvR)`（值包含 + 均匀假设）；
  3. `leftUnique`/`rightUnique`：唯一键推导（如 FK→PK 时 `sel=1/PK 行数`）；
  4. 单边 NDV：`1/该边 NDV`；
  5. 都没有：使用 `options.defaultSelectivity`（默认 0.1）并在 `warnings` 中**显著告警**。
- `options`（可选）：
  - `costModel`：`TOTAL_INTERMEDIATE_ROWS`（默认，各内部节点输出基数之和）或
    `SUM_OF_INPUTS`（每次连接的左右输入基数之和）；
  - `leftDeepOnly`：`true` 时只搜左深空间（响应附带 bushy 最优作对比）；
  - `defaultSelectivity`：缺失统计时的兜底选择率，∈ (0,1]；
  - `estimateCap`：基数/代价饱和上限，默认 `1e15`。

基数模型（与连接顺序无关）：

```
card(S) = ∏ rows(T) × ∏ selectivity(e)     // T ∈ S，e 的两端都在 S 内
```

响应给出 `optimalPlan`（`tree` / `steps` / `expression` / `leftDeepOrder` /
`totalCost` / `finalRows` / `costOverflow`）、全部连通子集的 `subsetCardinalities`、
`edgeSelectivity` 溯源，以及可能的 `warnings`。

最小示例：

```bash
curl -sS localhost:8080/plan -H 'Content-Type: application/json' --data '{
  "tables":[{"name":"A","rows":100},{"name":"B","rows":1000},{"name":"C","rows":10000}],
  "edges":[{"left":"A","right":"B","selectivity":0.1},
           {"left":"B","right":"C","selectivity":0.01}]}'
```

### 3.2 `POST /enumerate`

请求体同 `/plan`。返回 `legalLeftDeepOrders`（**每一条**合法左深排列及其代价）、
`legalLeftDeepOrderCount`，以及 `verification`：DP 代价、穷举的最小左深代价、
独立递归穷举的最小 bushy 代价、`dpMatchesBruteForce` 布尔值。
> 注：该接口会在响应里列出全部排列，适合 n 较小的核对；这正是自动化验收所用的机制。

### 3.3 `POST /simulate`

- `tables[].columns[]`：`distribution` ∈ `uniform | zipf | hotkey | sequence | frequencies`，
  以及 `ndv`、`skew`（Zipf 指数）、`hotFraction`、`offset`（把值域整体平移以制造不相交域）、
  `unique`、`frequencies`（显式频次）。
- `edges[]`：除表名外用 `leftColumn`/`rightColumn` 指定用表的第几列；可再给显式 `selectivity`。
- 顶层可选 `seed`（默认 42）、`maxIntermediateRows`（实际物化行数安全阀，默认 500 万）、
  `costModel`。

服务会：生成数据 → 用**实测 NDV + 均匀假设**得到统计模型并 DP 规划（`estimatedOptimal`）
→ 对每个连通子集**真实执行内存哈希连接**得到实测基数 → 用实测基数再 DP 规划
（`actualOptimal`）→ 用实测代价评估估计计划，给出 `comparison`
（`plansAgree`、`absoluteRegret`、`relativeRegret`）和每个子集的
估计/实际行数 `intermediateRows`。多谓词连接（环模式）作为同一连接上的多个等值条件处理。

### 3.4 一键复现全部样例

```bash
./scripts/run.sh 8080 &         # 先起服务
./scripts/run_samples.sh        # 默认 http://localhost:8080，结果写入 samples/results/
```

样例清单：

| 文件 | 演示点 |
|------|--------|
| `plan_chain3.json` | 三表链，逐步可解释计划 |
| `plan_tpch_like.json` | 五表类 TPC-H，FK→PK 唯一性推导，bushy 优于左深 |
| `plan_disconnected.json` | 断连图 → HTTP 422 + 分量计划 + 强制笛卡尔积代价 |
| `plan_missing_stats.json` | 缺失统计 → 默认选择率 + 告警 |
| `plan_overflow.json` | 基数超 1e15 → 饱和并标记 `costOverflow` |
| `enumerate_k4.json` | 完全图 K4，24 条全排列，DP/穷举核对 |
| `simulate_uniform.json` | 均匀数据：估计≈实际（3 表连接误差 <0.4%），两计划一致 |
| `simulate_skew.json` | 倾斜/不相交域：**估计最优 ≠ 真实最优**，相对遗憾 100% |

## 4. 算法要点

- **DP（`plan/Planner.java`）**：对每个连通子集掩码 M，枚举二分 M=L∪R
  （两侧均连通且至少有一条边跨过割），`cost(M)=min(cost(L)+cost(R)+nodeCost(L,R))`，
  支持全 bushy；`leftDeepOnly` 时限定 R 为单表。n≤8 时掩码至多 256，毫秒级。
- **连通性（`Estimator`/`GraphComponents`）**：每个子集从最低位表做 BFS 可达性；
  断连图在服务层显式拒绝，逐分量单独规划。
- **溢出**：所有基数乘法做饱和（clamp 到 `estimateCap`），杜绝 `Infinity` 毒化 DP 比较；
  受影响子集/计划带 `saturated`/`costOverflow` 标记。
- **真实执行（`sim/`）**：确定性随机数（固定 seed）；哈希连接以新增表为 build 端，
  环上的额外跨边作为等值过滤；行数超过 `maxIntermediateRows` 即中止并标 `capped`。

## 5. 自动化测试与验收结果

`./scripts/test.sh`（零依赖自研断言运行器）：

| 测试类 | 内容 |
|--------|------|
| `TestJson` | JSON 解析/转义/数字/往返 |
| `TestEstimatorAndPlanner` | 基数公式、NDV/唯一性推导、缺失告警、两种代价模型、零选择率 |
| `TestEnumerationCrossCheck` | **n=2..6、每规模 60 个随机连通实例（两种代价模型），枚举全部合法左深顺序 + 独立穷举 bushy，逐例核对 DP 取到全局最优**；另核 K4 的 24 排列 |
| `TestEdgeCases` | 参数校验、断连图、三种溢出/饱和、单表计划、无 NaN |
| `TestSimulation` | 各数据分布、均匀下估计≈实际、**倾斜下构造估计/真实计划翻转**、物化封顶 |
| `TestHttp` | 真实起服务，200/400/405/422 与各端点端到端 |

在本机实际运行（OpenJDK 17.0.20.1，Ubuntu 24.04，x86_64）：

```
PASS TestJson (14 checks)
PASS TestEstimatorAndPlanner (21 checks)
PASS TestEnumerationCrossCheck (1204 checks across randomized n=2..6 instances)
PASS TestEdgeCases (29 checks)
PASS TestSimulation (19 checks)
PASS TestHttp (15 checks)
All test classes passed.        # 全套约 2.4 秒
```

### 倾斜数据：估计最优 vs 真实最优（`simulate_skew.json`，已存档真实响应）

- A、B 在同一 Zipf 热键域（相关倾斜），B 到 C 的列值域被 `offset` 平移为完全不相交。
- 估计器依据给定选择率认为 A⋈B 便宜 → 选择左深 `[A,B,C]`，估计总代价 **5,184,000**。
- 真实执行：A⋈B = **3,000,000 行**（估计仅 64,000，低估约 47 倍）；B⋈C = **0 行**。
- 真实最优是一个 **bushy 计划**（先做产生 0 行的 B⋈C，再连 A），真实总代价 **3,000,000**；
  而估计器选出的计划真实代价为 **6,000,000**，`plansAgree=false`，
  **相对遗憾 = 100%**。这清楚区分了“估计最优”和“真实最优”。
- 对照组 `simulate_uniform.json`：3 表连接估计 1,600,000 vs 实际 1,604,707
  （比值 0.997），两个计划完全一致、遗憾为 0。

## 6. 已知限制 / 未完成项（如实记录）

1. 模拟器为教学级**内存哈希连接**：单表行数上限 20 万、中间结果默认 500 万行安全阀，
   超限只报告 `capped` 而不外排；不代表真实数据库执行引擎。
2. 统计模型采用经典**值包含 + 均匀分布/独立假设**；不维护直方图、相关系数，
   因此倾斜正是用 `/simulate` 专门暴露的盲区（这是演示目标，而非遗漏）。
3. 选择率只支持等值内连接（equi-join）；无外连接、非等值谓词、聚合/下推/并行代价，
   也不考虑索引扫描、随机/顺序 I/O 差异。代价是抽象行数代价，非绝对时间。
4. `/enumerate` 会把全部排列放进响应，n 很大时响应很大；它定位为小规模核对工具
   （规划本身 n≤8 无此问题）。
5. HTTP 服务为单实例、无鉴权/限流，绑定本机/容器内使用；未做持久化与优雅停机之外的运维特性。
6. 未提供 Maven/Gradle 包装：这是刻意的（零第三方依赖，`javac` 即可复现），
   若团队要求接入 CI 标准构建，可再加一个极薄的 `pom.xml`/`build.gradle`，不影响源码。
