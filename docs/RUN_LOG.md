# 运行记录（如实记录）

- 日期：2026-09-23（CST）
- 机器/系统：Linux 6.8.0-90-generic (Ubuntu 24.04)
- JDK：OpenJDK 21.0.12.1（仅用 `javac`/`java`，无 Maven/Gradle/第三方依赖）

## 1. 构建

命令：

```bash
rm -rf build
./scripts/build.sh
```

结果：成功。输出：

```
编译完成 -> build/classes
编译完成 -> build/classes
```

（脚本先编译 `src/main/java` 再以其为 classpath 编译 `src/test/java`，故打印两次。）

## 2. 自动化测试

命令：`./scripts/test.sh`
结果：**通过 21 / 21，退出码 0，无未通过项。**

```
PASS jsonParseAndSerializeRoundTrip
PASS jsonHandlesNumbersStringsNullEscape
PASS tableNdvComputedFromDataAndCapped
PASS cardinalityEstimationTextbookFormula
PASS estimationIsOrderIndependent
PASS dpPicksMinCostPlan_chain
PASS dpMatchesExhaustiveEnumeration
PASS legalTreeCountMatchesFormula
PASS disconnectedGraphMarksCartesianAndSemantics
PASS executorHashJoinSemantics
PASS executorMultiPredicateJoin
PASS nullKeysDoNotMatch
PASS numericStringCanonicalization
PASS skewedStatsEstimateVsActual
PASS nestedLoopCartesianSemantics
PASS allLegalPlansProduceIdenticalMultiset
PASS statsOnlySkipsExecution
PASS maxTablesLimit
PASS endToEndHandlerAndExport
PASS badRequestsFail
PASS zeroRowTable

通过 21 / 21
```

## 3. 样本批量运行

命令：`./scripts/run_samples.sh`（退出码 0）
响应 JSON 落在 `build/responses/<样本>.json`，导出件在 `build/out/<样本>/`。

### 3.1 chain4（4 表链，仅统计 + 穷举）

- `connected=true`，无笛卡尔积；估计最终 100,000 行，总代价 203,310。
- 穷举：`totalTrees=40, expectedTreeCount=40, minCost=203310, maxCost=601110,
  optimalCount=8, countMatchesFormula=true, dpCost=203310, matchesDp=true`。
- 说明：链式图不是完全图，{a,c} 等子集不可达，合法树数是 40（不是完全图的 120）；
  独立计数公式同样给出 40；DP 代价等于穷举最小值。

### 3.2 disconnected（5 表 3 个连通分量，含真实数据）

- 连通分量 `[orders,customers]`、`[products,categories]`、`[numbers]`；
  `connected=false, containsCartesian=true`。
- 计划含 2 个 `cartesian=true` 的 NESTEDLOOP 节点，分别在三个分量连完之后
  才做积；根节点 estRows=20，**actualRows=20，误差 0**。
- 穷举：48 棵合法树，精确公式 48，DP 最小代价 43，与穷举一致。

### 3.3 skew3（倾斜数据，关键验收项）

三张表各 100 行，连接键 NDV=10，键值 1 占 91 行、其余 9 个键各 1 行：

| 节点 | 估计行数 | 实际行数 |
| --- | --- | --- |
| 两表连接 t2⋈t3 | 1,000 | 8,290 |
| 三表连接 t1⋈(t2⋈t3) | 10,000 | 753,580 |

根节点相对误差 `(10000−753580)/753580 = −0.98673`（低估约 75.4 倍）。

### 3.4 uniform3（均匀对照组）

行数、NDV 与 skew3 完全相同，但 10 个键严格均匀（每键 10 行）：

- 两表估计=实际=1,000；三表估计=实际=10,000；`estimateError=0`。
- 结论：skew3 的失准来自均匀分布假设在倾斜数据上失效。

### 3.5 star8（8 表上限，事实表 1,000,000 行 + 7 维表，仅统计）

- `connected=true`，计划为 7 次纯哈希连接（维度从小到大依次接入事实表），
  无笛卡尔积节点；根估计 1,000,000 行，总代价 14,016,660。
- 表数 8 时不做穷举（选项默认关；枚举器硬性上限 6）。

### 3.6 其他手工验证的命令

```bash
# stdin 入口
cat samples/skew3.json | java -cp build/classes joinopt.Main -
# => ok=true, actual=753580, est=10000

# 导出目录
java -cp build/classes joinopt.Main samples/disconnected.json --export-dir build/out/disconnected
# => 生成 data.json / plan.json / plan.txt / request.json

# 错误请求退出码
echo '{"tables":[]}' | java -cp build/classes joinopt.Main -   # exit=1, {"error":"请求缺少 tables 或 tables 为空"}
java -cp build/classes joinopt.Main samples/不存在.json        # exit=1, {"error":"请求文件不存在: ..."}
```

## 4. 开发过程中出现并已修复的失败（如实记录）

1. **DP 子掩码枚举漏掉基准分区**：初版用 `for (lSub=rest; lSub!=0; …)`
   枚举，漏了 `lSub=0`（L 只含最低位一张表），2 表子集无计划，
   报 `DP 未为子集 3 生成计划`。已改为包含 0 的完整枚举，DP 与穷举器同步修复。
2. **穷举漏算左右朝向**：最初每个无向分区只组合一次，n=4 只数出 15 棵树
   （完全图应为 120）。根因是二叉连接计划的左右子节点有序。已对每个合法分区
   同时组合 (L,R) 与 (R,L)，并新增独立的计数 DP（`exactTreeCount`）互相印证。
3. **连通分量内部被凭空插入笛卡尔积**：最初“任意无跨边分区”都当作笛卡尔积，
   star8 出现“两个维度表先做积再连事实表”的计划。已引入 `Components.legalSplit`：
   同分量分区必须有等值谓词；跨分量分区两侧必须是完整分量之并。修复后 star8
   为纯哈希连接树，所有 DP/穷举测试通过。
4. **`rows: []` 被误判为仅统计表**：最初以 rows 是否为空判定 statsOnly，
   导致 0 行真实表被要求必须提供 rowCount。已改为按“是否提供 rows 字段”判定，
   `rows:[]` 是可执行的 0 行表（测试 `zeroRowTable`）。
5. **两个测试断言本身写错（引擎结果正确）**：
   - 误以为 4 表链的合法树数是完全图公式 120（实际链图合法树 40，与精确计数一致），
     已改为断言 40 并新增 K4 完全图=120 的对照；
   - 语义不变性测试中心算期望行数误写为 16（实际 a⋈b 为 4 行，×2×1=8），已改为 8。

## 5. 已知未通过 / 未覆盖项

- 无未通过的测试或样本（交付时 21/21 通过，5 个样本退出码均为 0）。
- 能力边界（非缺陷，见 README 第 7 节）：不支持外连接、非等值条件、投影/过滤/
  聚合；无直方图/相关关系统计，倾斜数据上估计必然失准；全内存物化，规模受限；
  无 HTTP/前端（纯后端 JSON 入口）。
