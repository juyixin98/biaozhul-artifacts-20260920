# 表达式谓词下推 —— 单机内存查询引擎（纯后端）

用 Java 从零实现的关系代数查询引擎，核心运算**全部自行编写**（嵌套循环连接、三值逻辑
过滤、投影），不调用任何现成 SQL 引擎。输入/输出走 JSON：给出基表数据与逻辑计划树，
引擎执行谓词下推改写，并输出改写前后的计划、**逐条改写理由**、结果与等价性校验。
数据与执行计划可导出。无前端。

## 1. 构建与运行

环境：JDK 21（仅用 `javac`/`java`，无 Maven/Gradle/第三方依赖）。

```bash
scripts/build.sh                 # 编译主代码到 out/
scripts/test.sh                  # 编译并运行全部自动化测试
scripts/run.sh samples/01_leftjoin_right_filter.json
scripts/run.sh samples/02_right_null_filter.json --export out/export.json
# 也可从标准输入：
cat samples/04_constant_false.json | java -cp out ppd.Main
```

## 2. 请求 / 响应格式

请求（见 `samples/`）：

```json
{
  "tables": [
    {"name": "l", "columns": ["id", "a"], "rows": [[1, 10], [2, null]]},
    {"name": "r", "columns": ["id", "b"], "rows": [[1, 100]]}
  ],
  "plan": {
    "op": "filter",
    "predicate": "r.b > 50",
    "child": {
      "op": "join", "joinType": "left",
      "on": ["l.id = r.id"],
      "left":  {"op": "scan", "table": "l"},
      "right": {"op": "scan", "table": "r"}
    }
  }
}
```

计划节点：

| op       | 字段 | 说明 |
|----------|------|------|
| scan     | `table` | 基表扫描 |
| filter   | `predicate`, `child` | 谓词过滤（三值逻辑，仅 TRUE 放行） |
| project  | `items[]`, `child` | 投影（列引用 / 常量 / 算术表达式） |
| join     | `joinType: inner|left`, `on[]`, `left`, `right` | 内连接 / 左外连接（左为保留侧） |

谓词语法：`= <> < <= > >=`、`AND OR NOT`、`IS [NOT] NULL`、`+ - *`、括号、
整数/小数/单引号字符串/`NULL TRUE FALSE`、`别名.列名` 限定引用。

响应关键字段：`equivalent`（改写前后结果包相等）、`rowCountBefore/After`、
`planBefore/After`（JSON 与文本）、`rewriteDecisions[]`（每条谓词的 `action` 与
中文 `reason`）、`rowsBefore/After`、`dataExport`（数据 + 前后计划，`--export` 落盘）。

## 3. 下推规则（列来源 + NULL 拒绝性）

重写器对 Filter 的合取谓词自底向上分类，**每条都给出理由**：

1. **Scan**：只引本表列的谓词下推为基表过滤；常量 `FALSE` 直接产生空结果。
2. **Project 穿透**：经**列来源**（origin 链）判定输出列是否为底层列的简单引用：
   是则把谓词改写为底层列名穿透投影；引用计算列（如 `l.a + r.b`）则保留在投影上方。
3. **InnerJoin**：
   - 只引左列 → 下推左子树；只引右列 → 下推右子树；
   - 跨两侧 → 并入 ON 条件（与连接后过滤等价）；
   - `FALSE` → 任一侧为空结果即空，落左子树。
4. **LeftJoin（左为保留侧，右为 NULL 补充侧）——禁止改变保留行语义**：
   - 只引保留侧列：安全下推到左子树（保留行左列永不为补 NULL）；
   - 引用右侧列时做 **NULL 拒绝性测试**：把谓词引用的右列集合整体置为 NULL
     （模拟未匹配的保留行），在三值逻辑下求谓词是否可能为 TRUE：
     - **拒绝 NULL**（不可能为 TRUE，如 `r.b>50`、`r.b IS NOT NULL`）：
       保留行必被该谓词过滤，因此先把 **LeftJoin 降级为 InnerJoin**，再下推右表 / 并入 ON；
     - **不拒绝 NULL**（如 `r.b IS NULL`、`r.b=1 OR r.b IS NULL`）：**必须留在连接上方**，
       下推会删掉本应保留的左行；
   - 跨两侧谓词同样需要拒绝右表 NULL 才允许“降级 + 并入 ON”，否则留在上方；
   - `FALSE` 只能落保留侧——推到右侧会让右表为空、反而把所有左行变成保留行。

NULL 拒绝性由 `Expr.rejectsNulls(列集合)` 用抽象解释实现（`T/F/U` 三比特传播），
支持 `AND/OR/NOT/IS NULL/比较/算术` 的精确组合判定，而不是只看谓词表面形态。

## 4. 自动化测试与验收

`scripts/test.sh` 运行 `src/`+`test/` 下的自建断言框架（无 JUnit 依赖）：

- 表达式：解析往返、三值逻辑真值表、NULL 拒绝性 20 余例；
- 执行器：过滤/投影/内连接/左外连接、ON 与 WHERE 在左连接上的差异、重名列歧义报错、
  JSON 入口；
- **穷举验收（`ExhaustiveTest`）**：两张 2 行小表，每单元格取 `{NULL,1,2}`，
  各 3⁴=81 种取值，共 6561 个数据库；在 21 类查询（左/内连接 × 12/6 种谓词 +
  投影穿透/阻断 + 计算列）上比较改写前后结果——
  **137,781 个组合，0 个不一致**；并对关键场景断言实际计划形态与决策动作。
- 明确覆盖验收点名场景：
  - **右表 NULL 过滤**：`r.b IS NULL` 留在外连接上方（`02_right_null_filter.json`，
    手工真值：命中右值本身为 NULL 的匹配行 + 未匹配补 NULL 保留行，共 2 行）；
  - **重名列**：`l.x`/`r.x` 必须限定名消歧，裸 `x` 报歧义错误（`03_duplicate_columns.json`）；
  - **常量假谓词**：`FALSE` 只落保留侧（`04_constant_false.json`）。

最近一次实际运行结果见 `docs/test-output.txt`（95 条断言全部通过，退出码 0）；
六个样例的完整响应与导出落在 `docs/sample-output/`。

## 5. 目录结构

```
src/ppd/            主代码
  Json/Expr/ExprParser   自建 JSON、表达式树与递归下降解析器（三值逻辑 + 抽象解释）
  Plan/PlanExplain       计划树（Scan/Filter/Project/InnerJoin/LeftJoin）与计划导出
  Executor               嵌套循环连接、过滤、投影执行器
  PushdownRewriter       谓词下推重写器（列来源 + NULL 拒绝性，输出 Decision 理由）
  QueryEngine/Main       JSON 请求门面与 CLI 入口
test/ppd/tests/     自动化测试（Assert/ExprTest/ExecutorTest/ExhaustiveTest）
samples/            六个 JSON 请求样例
scripts/            build.sh / test.sh / run.sh
docs/               实际测试输出与样例响应/导出存档
```

## 6. 已知边界（如实说明）

- 仅实现 Filter / Project / InnerJoin / LeftJoin / Scan；未实现右外/全外连接、
  聚合、排序、分组、相关子查询；表无统计信息，不做基于代价的选择。
- 投影仅支持列引用、常量、算术表达式的输出；谓词对计算列不下推（按规则保留上方）。
- ON 条件本身不在本轮改写范围内（不把 ON 谓词推入子树）；WHERE 上的谓词改写完备。
- 基表列统一按“可空”对待；连接键含 NULL 的行为遵循 SQL（NULL 不匹配）。
