# 表达式谓词下推（Expression Predicate Pushdown）

纯后端、零第三方依赖的 Java 单机内存查询引擎。核心运算（过滤、投影、内连接、左连接、三值逻辑谓词求值）全部自行实现，不调用任何现成 SQL 引擎。提供 JSON-over-HTTP 请求入口，支持执行计划与结果数据导出，并对每个谓词下推决策输出改写理由。

## 构建与运行

需要 JDK 17+（开发验证使用 OpenJDK 21）。

```bash
./build.sh                 # 编译到 out/
./run_tests.sh             # 编译并运行全部自动化测试（含穷举验收）
java -cp out com.pushdown.api.Server 8080   # 启动 HTTP 服务（默认端口 8080）
```

## 体系结构

```
src/main/java/com/pushdown/
├── json/Json.java        最小 JSON 解析/序列化（无外部依赖）
├── expr/Expr.java        表达式树：字面量/列引用/比较/算术/AND/OR/NOT/IS NULL，
│                         SQL 三值逻辑求值（TRUE/FALSE/UNKNOWN）
├── plan/Plan.java        计划树：Scan / Filter / Project / Join(INNER, LEFT)
├── exec/Engine.java      内存执行器：过滤、投影、嵌套循环内连接与左连接
├── opt/Optimizer.java    谓词下推优化器（核心），输出每条改写理由
└── api/                  JSON 计划互转 + JDK 内置 HttpServer 入口
src/test/java/com/pushdown/TestRunner.java   测试入口（无测试框架，失败即非零退出）
```

## 下推规则

优化器对每个合取项（AND 拆分后）做两项分析：

- **列来源（provenance）**：谓词引用的列属于连接的哪一侧；
- **NULL 拒绝性（null-rejecting）**：当某侧列全为 NULL 时，谓词是否仍可能为 TRUE（保守静态分析，见下表）。

| 场景 | 决策 |
|---|---|
| INNER JOIN，只引用左/右单侧 | 下推到对应侧 |
| INNER JOIN，常量谓词（含 `1=0` 折叠后的 FALSE） | 下推到左输入（左侧为空则连接为空，与上层过滤等价） |
| LEFT JOIN，只引用左侧 | 下推到左输入 |
| LEFT JOIN，只引用右侧且 NULL 拒绝 | **LEFT JOIN 退化为 INNER JOIN**，谓词推入右输入 |
| LEFT JOIN，只引用右侧但非 NULL 拒绝（如 `U.C IS NULL`） | 保留在连接之上，理由 `kept-for-null-preservation` |
| LEFT JOIN，常量谓词 | 保留在连接之上（推入任一侧都会改变保留行语义） |
| 引用双侧 | 保留在连接之上 |
| 穿越 Project | 不下推 |

NULL 拒绝性判定（保守）：比较/算术运算引用了该侧列 → 拒绝；`IS NOT NULL` → 拒绝；`IS NULL` → 不拒绝；`AND` 任一拒绝则拒绝；`OR` 两侧都拒绝才拒绝；常量 FALSE → （空真）拒绝，常量 TRUE → 不拒绝。

**关键正确性点**：对 LEFT JOIN 右表的 NULL 拒绝谓词，不能简单把 Filter 推到右表下方而保留 LEFT JOIN——那样未匹配的左行会以全 NULL 保留行的形式绕过过滤凭空出现。正确改写是教科书规则：NULL 拒绝谓词使外连接**退化为内连接**（`Filter(p, T⋉U)` ≡ `T⋈Filter(p, U)`，p 为右表 NULL 拒绝谓词）。本项目初版实现曾犯此错误，被穷举验收测试捕获后修正（见"验证记录"）。

## JSON 请求格式

`POST /query`：

```json
{
  "tables": {"T": {"columns": ["A","B"], "rows": [[1,1],[2,null]]}},
  "plan":   {"type": "filter", "pred": {...}, "child": {...}},
  "optimize": true,
  "exportDir": "out/export-demo"
}
```

- 表达式：`{"lit": v}`、`{"col": "T.A"}`（必须限定表名，支持重名列）、`{"op": "=|!=|<|<=|>|>=|+|-|*", "args": [l,r]}`、`{"op":"and|or","args":[...]}`、`{"op":"not|isnull|isnotnull","args":[e]}`
- 计划节点：`scan` / `filter` / `project` / `join`（`joinType`: `inner`|`left`）
- `optimize` 缺省为 `true`；`exportDir` 可选，指定后把执行计划写入 `plan.json`、结果数据写入 `data.json`（即"数据与执行计划可导出"）

响应：`originalPlan`、`optimizedPlan`、`reasons`（每条含 `rule` 与 `detail`）、`schema`、`rows`。

## 请求样例

`samples/` 下四个样例，启动服务后可这样执行：

```bash
java -cp out com.pushdown.api.Server 18080 &
curl -s -X POST -H 'Content-Type: application/json' \
     --data @samples/request1_left_null_rejecting.json http://127.0.0.1:18080/query
```

1. `request1_left_null_rejecting.json` — LEFT JOIN + 右表 NULL 拒绝谓词 `U.C=10`：LEFT 退化为 INNER，谓词推入右表；
2. `request2_left_isnull_kept.json` — LEFT JOIN + `U.C IS NULL`：非 NULL 拒绝，保留在连接之上；
3. `request3_constant_false.json` — INNER JOIN + 常量假 `1=0`：先折叠为 `false`，再推入左输入，结果为空；
4. `request4_and_export.json` — AND 拆分后双侧分别下推，并把计划与结果导出到 `out/export-demo/`。

## 验证记录（实际运行，如实记录）

环境：OpenJDK 21.0.12.1，Ubuntu（Linux 6.8）。

### 自动化测试

命令：`./run_tests.sh`

最终结果：

```
build ok
exhaustive: 16900 table-pairs x 2 join types x 10 predicates = 338000 comparisons, mismatches=0
passed=37 failed=0
ALL TESTS PASSED
```

穷举验收方法：域 `{NULL,1,2}`，两列行共 9 种，枚举所有 0~3 行的表（130 张），两两组合（16900 对）× 2 种连接 × 10 个谓词（覆盖右表 NULL 过滤 `U.C=1`/`U.C IS NULL`/`IS NOT NULL`、重名列 `T.B`/`U.B`、常量假 `false` 与 `1=0`、双侧引用、AND/OR 组合），逐组比较改写前后结果（行多重集），338000 组全部一致。

**开发过程中被发现并修复的问题（如实记录）**：初版把 LEFT JOIN 右表的 NULL 拒绝谓词直接推到右表下方且保留 LEFT JOIN，穷举测试报出 40882 组失配（如 `T=[(NULL,NULL)], U=[]` 时改写后多出一行全 NULL 保留行）。随后改为正确规则"NULL 拒绝谓词使 LEFT JOIN 退化为 INNER JOIN"，复测 0 失配。除此之外无其他未通过项。

### HTTP 样例（实际输出摘要）

- 样例 1：`optimizedPlan` 中 `joinType` 变为 `inner`，右输入套上 `filter U.C=10`；`reasons` 含 `push-right-null-rejecting`；结果 `[[1,1,1,10]]`。
- 样例 2：计划未改动；`reasons` 含 `kept-for-null-preservation`；结果 `[[2,2,2,null],[3,null,null,null]]`（保留行正确参与 `IS NULL` 过滤）。
- 样例 3：`reasons` 依次含 `constant-fold`、`constant-pushed-inner`；结果 `[]`。
- 样例 4：AND 两项分别下推左右两侧；`out/export-demo/plan.json` 与 `data.json` 成功写出。

全部测试通过，无遗留未通过项。
