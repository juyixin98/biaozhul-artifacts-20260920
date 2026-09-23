# JSON 请求 / 响应协议

所有字段名区分大小写。数值中整数与浮点数都可接受；连接键比较时
整数 `1` 与浮点 `1.0` 视为相等，字符串 `"1"` 与数字不相等。

## 1. 请求

```json
{
  "tables": [ ... ],
  "joins":  [ ... ],
  "options": { ... }
}
```

### tables（必填，1–8 个）

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `name` | 是 | 表名，请求内唯一 |
| `columns` | 是 | 列名数组，表内唯一 |
| `rowCount` | 仅统计表必填 | 行数。提供 `rows` 时可省略（以实际行数为准，不一致仅告警） |
| `ndv` | 否 | 各列不同值个数。对象 `{列名: 个数}` 或与 columns 对齐的数组；省略时有数据按实计算、无数据假设每列 NDV=行数。超过行数会被截断并告警 |
| `rows` | 否 | 数据行数组，每行长度必须等于列数。**不提供**表示仅统计表；显式 `[]` 表示 0 行真实表 |

### joins（可选；省略时全部表做笛卡尔积并告警）

每个谓词两种等价写法：

```json
["orders", "cust_id", "customers", "id"]
```

```json
{ "leftTable": "orders", "leftColumn": "cust_id",
  "rightTable": "customers", "rightColumn": "id" }
```

两端必须是不同的表且列必须存在。同一分区上的多条谓词在执行时构成复合等值键。

### options（全部可选）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `execute` | `true` | 是否真实执行。任一表仅统计时自动跳过并告警 |
| `enumerate` | `false` | 是否穷举全部合法计划（>6 表自动跳过并告警） |
| `resultLimit` | `20` | 响应中回显的结果行数上限（完整结果仍写入导出的 data.json） |

## 2. 响应（成功）

```json
{
  "ok": true,
  "summary": { ... },
  "plan": { ... },
  "planText": "...",
  "enumeration": { ... },
  "result": { ... },
  "subsets": [ ... ],
  "warnings": [ "..." ],
  "exportedTo": "/abs/path"
}
```

- `summary`：`tableCount`、`joinPredicateCount`、`connectedComponents`
  （每个分量一个表名数组）、`connected`、`containsCartesian`、`estRows`、
  `totalCost`；执行后另有 `actualRows`、`estimateError`（(估计−实际)/实际）。
- `plan`：递归计划树。
  - 叶子：`{"kind":"table","table":"a","tableIndex":0,"tables":["a"],
    "estRows":100,"cost":0}`
  - 连接：`{"kind":"hashjoin"|"cartesian","cartesian":false,
    "predicates":[{"left":"a.b_id","right":"b.id"}],
    "children":[左,右],"tables":[...],"estRows":...,"cost":...,
    "actualRows":...}`（执行后才有 actualRows）
- `enumeration`：`totalTrees`（穷举树数）、`expectedTreeCount`（精确计数）、
  `minCost`/`maxCost`、`optimalCount`（最优树数量）、
  `countMatchesFormula`、`dpCost`、`matchesDp`（穷举最小代价==DP 代价）。
- `result`：`columns`（全限定名）、`rowCount`、`rowsShown`、`rows`（截断后）、
  `estRows`、`estimateVsActual`（`estimated/actual/absoluteDiff/relativeError`）。
- `subsets`：每个非空表子集 `{tables:[...], estRows:N}`，
  可用来直接核对同一子集估计与分区方式无关。

## 3. 错误响应

任何解析/校验/执行错误：进程退出码 1，stderr 输出：

```json
{ "error": "中文错误说明" }
```

常见错误：表数 >8、表名重复、列不存在、谓词引用未知表、数据行列数不符、
仅统计表缺少 `rowCount`、请求文件不存在等。

## 4. 导出目录（`--export-dir DIR`）

| 文件 | 内容 |
| --- | --- |
| `plan.json` | 计划树 + 文本计划 + 连通分量 |
| `plan.txt` | 缩进文本计划（actualRows 执行后回填） |
| `data.json` | 全部输入表（列、行数、NDV、原始数据行）+ 完整执行结果（不截断） |
| `request.json` | 解析后回显的请求（键顺序规范化） |
