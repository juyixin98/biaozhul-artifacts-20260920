# 连接顺序优化（Join Order Optimization）纯后端原型

单机、内存、零外部依赖的连接顺序优化器与查询执行原型。核心运算全部自行实现
（JSON 解析、动态规划选序、穷举校验、基数估计、哈希连接 / 嵌套循环执行），
**不依赖任何 SQL 引擎或第三方库**，只用 JDK 21 的 `javac/java` 即可构建运行。

---

## 1. 它能做什么

1. **最多 8 张表的内等值连接（INNER EQUI-JOIN）动态规划选序**
   - System R 风格自底向上子集 DP，位掩码表示表集合（8 表仅 256 个子集）。
   - 代价基于各基表**行数**与每列**不同值个数（NDV）**的经典均匀分布估计。
2. **断开的连接图（disconnected join graph）**
   - 自动求连接图连通分量；分量之间没有等值谓词，完整计划必然包含笛卡尔积。
   - 笛卡尔积节点显式标记 `cartesian: true`（算子为 NESTEDLOOP CROSS JOIN），
     且只允许出现在**完整连通分量之间**，不会把一个连通分量拆散后凭空做积。
3. **小规模穷举校验**（≤6 表）
   - 枚举所有合法二叉连接树（左右有序），与 DP 的最优代价逐项对照；
     合法树数另有独立的计数 DP 精确公式相互印证。
4. **估计 vs 实际**
   - 有 `rows` 数据的表可真实执行计划（内存哈希连接），回填每个节点的真实行数；
   - 提供倾斜样本（`skew3`）与均匀对照样本（`uniform3`），展示均匀假设的失准。
5. **语义不变性**：估计只影响代价与顺序。所有合法顺序表达同一段关系代数，
   执行器对每棵合法计划树产生完全一致的结果行多重集（自动化测试逐行断言）。
6. **数据与计划可导出**：`--export-dir` 导出 `plan.json` / `plan.txt` /
   `data.json`（含输入表与完整结果）/ `request.json`（原始请求回显）。

不做：前端、HTTP 服务端、投影 / 过滤 / 聚合 / 外连接 / 非等值连接、
基于直方图或采样的高级统计。

---

## 2. 目录结构

```
src/main/java/joinopt/
  Json.java         零依赖 JSON 解析/序列化 + 类型取值辅助
  Table.java        表定义：列、行数、NDV、实际数据行；NDV 截断/默认/实算
  JoinPred.java     等值连接谓词 t.a = u.b
  Stats.java        中间关系统计快照（估计行数 + 各列 NDV）
  Estimator.java    基数估计（只用基表统计 => 顺序不变性）
  Components.java   连接图连通分量 + “合法分区”判定（DP 与穷举共用）
  PlanNode.java     计划树（table / hashjoin / cartesian）、代价、JSON/文本序列化
  Optimizer.java    子集动态规划，输出最优计划
  Enumerator.java   合法计划穷举器 + 合法树数精确计数（校验用）
  Rel.java          执行期中间关系（全限定列名 + 物化行）
  Executor.java     内存执行器：HASH JOIN（复合键哈希表）/ CARTESIAN（嵌套循环）
  Main.java         JSON 请求入口（文件或 stdin）、响应组装、导出
src/test/java/joinopt/
  TestFramework.java  极简断言框架
  AllTests.java       21 个自动化测试
samples/            请求样例（star8/skew3/uniform3 由脚本生成）
scripts/            build.sh / test.sh / run.sh / run_samples.sh / gen_samples.py
docs/PROTOCOL.md    请求/响应 JSON 字段说明
docs/RUN_LOG.md     本机实际构建、运行、测试记录（如实记录，含开发中遇到的失败）
```

---

## 3. 快速开始

要求：JDK（在 OpenJDK 21 上开发验证；仅用标准 API，17+ 应可编译）。

```bash
# 编译（主代码 + 测试，输出到 build/classes/）
./scripts/build.sh

# 运行自动化测试（21 个用例）
./scripts/test.sh

# 对单个请求文件运行，响应 JSON 打到 stdout
./scripts/run.sh samples/skew3.json

# 从标准输入读取
cat samples/chain4.json | ./scripts/run.sh -

# 运行并导出计划与数据
./scripts/run.sh samples/disconnected.json --export-dir build/out/disconnected

# 批量跑全部样本（含重新生成 star8/skew3/uniform3），输出汇总与导出件
./scripts/run_samples.sh
```

无需 Maven/Gradle，无任何需要下载的依赖。

---

## 4. 请求与响应（摘要，完整字段见 [docs/PROTOCOL.md](docs/PROTOCOL.md)）

请求：

```json
{
  "tables": [
    { "name": "orders",
      "columns": ["id", "cust_id"],
      "rows": [[1, 1], [2, 1], [3, 2]],
      "ndv": { "id": 3, "cust_id": 2 } }
  ],
  "joins": [
    ["orders", "cust_id", "customers", "id"]
  ],
  "options": { "execute": true, "enumerate": true, "resultLimit": 20 }
}
```

- 表可只给统计（`rowCount` + `ndv`，无 `rows`），此时只优化不执行；
  给出 `rows` 时 `rowCount`/`ndv` 可省略，按实际数据计算（`rowCount` 不一致会告警并以数据为准；
  注意 `rows: []` 表示 **0 行真实表**，与“不提供 rows”不同）。
- 谓词可用 4 元素数组 `[表,列,表,列]` 或对象（`leftTable/leftColumn/rightTable/rightColumn`）。
- 响应顶层含：`summary`、`plan`（JSON 计划树）、`planText`（缩进文本树）、
  `enumeration`（穷举校验，可选）、`result`（执行结果与估计/实际对比，可选）、
  `subsets`（所有表子集的估计行数）、`warnings`。

文本计划示例（断开图，见 `samples/disconnected.json`）：

```
NESTEDLOOP CROSS JOIN  [cartesian=true, estRows=20, cost=43]  actualRows=20
├─ HASH JOIN ON orders.cust_id=customers.id  [estRows=5, cost=13]  actualRows=5
│  ├─ SCAN customers  [rows=3]
│  └─ SCAN orders  [rows=5]
└─ NESTEDLOOP CROSS JOIN  [cartesian=true, estRows=4, cost=10]  actualRows=4
   ├─ HASH JOIN ON products.cat=categories.cat  [estRows=2, cost=6]  actualRows=2
   │  ├─ SCAN categories  [rows=2]
   │  └─ SCAN products  [rows=2]
   └─ SCAN numbers  [rows=2]
```

---

## 5. 算法与代价模型

### 5.1 基数估计（只用基表统计）

对表集合 S，令 E(S) 为两端都落在 S 内的等值谓词：

```
|⋈ S| = Π_{t∈S} |t| × Π_{(t.a=u.b)∈E(S)} 1 / max(NDV(t.a), NDV(u.b))
```

- 每条连接边选择率 `1/max(两侧 NDV)`：经典等连接均匀、独立假设；取 max 使
  外键（NDV 小）连主键（NDV 大）时估计不超过外键侧行数，保守自洽。
- 结果列 NDV 截断为 `min(基表 NDV, 估计结果行数)`。
- 该式**只依赖基表统计**，因此同一子集无论按什么树计算，估计完全相同
  （顺序不变性，测试 `estimationIsOrderIndependent` 断言）。

### 5.2 节点代价（累计到根）

- 叶子 SCAN：0（基表扫描成本不计入相对比较）。
- HASH JOIN：`|L| + |R| + |L⋈R|`（建哈希 + 探测 + 输出的抽象 I/O 单位）。
- CARTESIAN（嵌套循环）：`|L| × |R|`（每个配对都要产生）。
- 计划总代价 = 树中各节点代价之和；优化器在全部合法树中最小化它。

### 5.3 DP 与“合法分区”

`best[mask]` = 覆盖该表集合的最小累计代价计划。枚举无向二分分区，
每个分区再考虑左右两种朝向（不同朝向是不同的物理树）。分区合法性（`Components.legalSplit`）：

- 集合落在**同一个连接图分量**内：分区两侧之间**必须存在等值谓词**
  （哈希连接）——不允许在一个连通分量内部凭空插入笛卡尔积；
- 集合跨越多个分量：两侧必须各自是**若干完整分量的并**（分量不可拆散），
  此时节点是笛卡尔积。

这正是 System R “只枚举连通访问路径”的经典约束。它带来两个结果：

- 连通查询的计划是纯哈希连接（如 `star8` 的逐级星型连接）；
- 断开查询的笛卡尔积被代价模型安排在中间结果最小的位置
  （`disconnected` 中先各自连完，再做分量间的积）。

### 5.4 穷举校验

≤6 表时可递归枚举全部合法二叉连接树并取最小代价，与 DP 对照；
合法树数量由另一个只计数、不建树的子集 DP（`Enumerator.exactTreeCount`）精确给出，
三方（穷举数 / 计数公式 / DP 最优代价）在响应 `enumeration` 中同时给出。
作为特例：图完全连通（K_n）或完全无边时，合法树数为
`f(n) = (2n−2)!/(n−1)!`（n=1..6：1, 2, 12, 120, 1680, 30240）。

### 5.5 执行语义

- HASH JOIN：右侧建复合键哈希表、左侧探测；多谓词作为复合等值键同时成立；
  `NULL` 不匹配任何值（含另一个 NULL，SQL 语义）；数值 1 与 1.0 视为相等，
  字符串 `"1"` 不与数字 1 匹配。
- CARTESIAN：双层循环生成全部配对。
- 无投影：输出列为左右输入列拼接。不同计划的行排列可能不同，行多重集相同。

---

## 6. 验收点与对应样本/测试

| 验收要求 | 对应内容 |
| --- | --- |
| ≤8 表内等值连接 DP，基于行数/NDV 估代价 | `Optimizer`/`Estimator`，样本 `chain4.json`、`star8.json`（8 表上限） |
| 小规模枚举所有合法顺序，验证最小估计代价 | `Enumerator` + `enumerate:true`；测试 `dpMatchesExhaustiveEnumeration`、`legalTreeCountMatchesFormula` |
| 断开连接图 + 标记笛卡尔积，估计不改语义 | `Components`、样本 `disconnected.json`；测试 `disconnectedGraphMarksCartesianAndSemantics` |
| 倾斜统计展示估计与实际行数差异 | 样本 `skew3.json`（低估约 75 倍）与对照组 `uniform3.json`（误差 0） |
| 数据和执行计划可导出 | `--export-dir`：plan.json/plan.txt/data.json/request.json |
| 自动化测试 + 如实记录 | `./scripts/test.sh`（21 用例）；[docs/RUN_LOG.md](docs/RUN_LOG.md) |

### 倾斜实验数字（n=100，NDV=10，热点键 1 占 91 行，其余 9 键各 1 行）

| | 均匀假设估计 | 实际执行 | 偏差 |
| --- | --- | --- | --- |
| 两表连接 | 1,000 | 8,290 | 低估 8.29 倍 |
| 三表连接 | 10,000 | 753,580 | 低估 75.36 倍（相对误差 −98.67%） |

同样的行数与 NDV、键值严格均匀时（`uniform3`）估计与实际**完全相等**，
说明差异源于均匀分布假设在倾斜数据上失效，而非估计公式或执行器的实现错误。

---

## 7. 限制与设计取舍

- 只支持内连接、等值条件、无投影/过滤/聚合；目标是把“选序 + 估计 + 执行”
  这条主干做到可验证，而非一个完整数据库。
- 统计模型是最基础的均匀/独立假设，无直方图、相关关系、外键完整性知识；
  倾斜场景必然失准（这正是 `skew3` 要展示的）。
- 全部中间结果物化在内存，笛卡尔积/大连接结果会真实占用内存，样本规模刻意保持小。
- 无网络接口：JSON 请求经文件或 stdin 进入（题目为纯后端、JSON 请求入口）。
