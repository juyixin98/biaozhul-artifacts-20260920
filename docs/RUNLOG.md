# 运行记录（RUNLOG）

本文件如实记录项目实际执行过的命令、结果，以及开发过程中**实际出现过的失败
与修复**。所有“通过”结论均可由文末命令在本机复现。

- 记录时间（UTC）：2026-09-23T02:09Z
- 操作系统：Linux 6.8.0-90-generic x86_64（Ubuntu 24.04）
- 实际使用 JDK：
  - `java -version` → openjdk **21.0.12.1**
  - `javac -version` → javac **21.0.12.1**
  - 代码仅使用 Java 17 语言特性，README 声明要求 JDK 17+；本机实际用 21 编译运行通过。
- 未使用 Maven/Gradle 或任何第三方依赖；`build/` 为 `javac` 输出目录。

---

## 1. 构建

命令：

```bash
bash build.sh
```

结果：**成功（退出码 0）**。输出：

```
==> 清理 build/
==> 编译主代码 -> build/classes
==> 编译测试代码 -> build/test-classes
==> 编译完成
```

---

## 2. 自动化测试

命令：

```bash
bash run-tests.sh
# 等价于：java -cp build/classes:build/test-classes tvl.TestRunner
```

结果：**全部通过，退出码 0**。完整输出：

```
[穷举三值真值表]   (39 通过)
[NULL 参与比较]   (132 通过)
[运算符优先级]   (49 通过)
[短路求值]   (32 通过)
[整数溢出与除零]   (56 通过)
[错误位置]   (59 通过)
[类型检查]   (30 通过)
[JSON 解析器]   (23 通过)
[JSON 请求端到端]   (54 通过)
===================================
合计通过：474，失败：0，耗时 114 ms

-----------------------------------
测试通过：474，失败：0
```

该完整输出同时保存在 `docs/output/test-run.txt`。

---

## 3. 样例查询实际运行

命令格式：

```bash
java -cp build/classes tvl.cli.Main query samples/<file>.json
```

实际运行结果（`docs/output/<file>.response.json` 为完整真实响应）：

| 样例 | 退出码 | 结果摘要 |
|---|---|---|
| `query-basic.json` | **0** | 5 行：TRUE 2 / FALSE 2 / UNKNOWN 1，选中 2 行 `[2,10]`、`[9,0]` |
| `query-null-comparison.json` | **0** | `a = 1 OR a IS NULL`：NULL 行经 IS NULL 被选中（UNKNOWN 比较被 OR 救回） |
| `query-short-circuit.json` | **0** | `flag OR (10/den=2)`：`[true,0]` 行**短路未除零**，选中 2 行 |
| `query-string-bool-export.json` | **0** | 字符串比较 + 布尔列；`includeTable:true` 回带全部数据 |
| `query-mixed-logic.json` | **0** | 4 行：TRUE 1 / FALSE 2 / UNKNOWN 1 |
| `error-type-mismatch.json` | **2** | `TYPE_MISMATCH`，position offset=2 column=3（指向 `+`） |
| `error-unknown-column.json` | **2** | `UNKNOWN_COLUMN`，position offset=0 length=6（指向 `amount`） |
| `error-division-by-zero.json` | **2** | `DIVISION_BY_ZERO`（第 2 行 `den=0`） |
| `error-integer-overflow.json` | **2** | `INTEGER_OVERFLOW`（`9223372036854775807 + 1`） |

短路样例（关键验收）的真实结果片段：

```json
"selectedRows": [[true, 0], [false, 5]]
"triStats": {"TRUE": 2, "FALSE": 1, "UNKNOWN": 0}
```

`[true,0]` 这一行除数为 0，但因左操作数 `flag=TRUE` 触发 OR 短路，
右侧 `10/den` 根本未求值，因此**没有报错且该行被选中**。

其他手工验证命令与结果：

```bash
# 标准输入
echo '{"expression":"1 + 2 * 3"}' | java -cp build/classes tvl.cli.Main query
#   -> ok=true，result.value = INTEGER 7（证明 * 优先于 +），退出码 0

# 穷举真值表
java -cp build/classes tvl.cli.Main truth-table
#   -> 输出 NOT(3) / AND(9) / OR(9) 共 21 行，退出码 0；保存于 docs/output/truth-table.json

# 非法 JSON
echo '{bad' | java -cp build/classes tvl.cli.Main query
#   -> ok=false, code=INVALID_JSON，退出码 2

# 纯语法 AST（含列名，不做类型检查）
java -cp build/classes tvl.cli.Main parse "a > 1 AND NOT b IS NULL"
#   -> 根节点 Binary(op=AND)，退出码 0
```

---

## 4. 开发过程中实际出现、并已修复的问题（未通过项历史）

以下问题都在开发自测中真实出现过测试失败，随后修复并由相应测试回归锁定。
**当前代码中这些问题均已不存在**，此处按“如实记录”要求保留：

1. **词法器把关键字 `double` 当成方法名**
   初版把“读取双字符操作符”的辅助方法命名为 `double(...)`，`double` 是 Java
   关键字，首次编译即失败（多处 `not a statement`）。
   修复：重命名为 `twoChar(...)`。

2. **乘法溢出检测在 `Long.MIN_VALUE * -1` 时漏检（真实运算 bug）**
   初版手写检测用 `r / b != a` 反推，但该情形下除法本身也回绕，导致漏判，
   `OverflowTest` 报“期望抛出 INTEGER_OVERFLOW，但正常返回”。
   修复：改用 `Math.multiplyExact / addExact / subtractExact / negateExact`
   捕获 `ArithmeticException`，再转换为自定义错误码 `INTEGER_OVERFLOW`；
   除法的除零与 `MIN / -1` 单独显式判断。回归测试
   `OverflowTest.mulOverflow()` 锁定。

3. **非结合比较检测时机错误（真实语法 bug）**
   初版在解析右操作数**之前**检查下一个 token，`1 < 2 > 3` 解析出右操作数后
   `>` 才暴露，导致误判为 `SYNTAX_ERROR` 而非 `NON_ASSOCIATIVE_COMPARISON`。
   修复：将检查移到右操作数解析之后。回归测试
   `ErrorPositionTest.nonAssociativeComparison()` 锁定。

4. **JSON 解析器在输入提前结束时抛 `StringIndexOutOfBoundsException`（真实健壮性 bug）**
   `peek()` 不判 EOF，解析 `{'a':1}`、`[1,]`、`""(空)` 等畸形输入时抛底层
   越界异常而非 `JsonParseException`。
   修复：`peek()` 在越界时抛带行列号的 `JsonParseException("意外结束")`。
   回归测试 `JsonParserTest.parseErrors()` 锁定。

5. **执行计划导出把 `String[]` 直接序列化**
   `TableScan.columns` 放入 Java 数组，手写序列化器将其输出成
   `"[Ljava.lang.String;@2626b418"`（不可用）。
   修复：用 `Arrays.asList(...)` 转为 List。回归测试
   `JsonRequestTest.astAndPlanExport()` 增加对 `columns` 内容的断言锁定。

6. **测试期望写错（非引擎 bug），共 3 处**，均据 SQL/语言语义更正测试：
   - `7 / -3`：整除向零截断应为 `-2`，原误写为 `2`；
   - 整数字面量 `9223372036854775807` 就是 `MAX`，原误当作 `MIN+1`；
   - `a AND b +` 的 EOF offset：字符串长 9，原误写为 7。

7. **测试计数显示为 0（测试框架自身显示问题）**
   初版只有经 `TF.test(...)` 包装的用例才累计“通过数”，各套件直接调用底层
   断言，导致所有套件都打印“(0 通过)”（虽然退出码 0 且无失败）。
   修复：在每条成功的底层断言中累计计数；当前 474 条断言计数正确。

8. **源码直接书写 `-9223372036854775808` 的处理（设计决策，非缺陷）**
   词法器把 `9223372036854775808` 当作整数 token，超出正数范围，编译期即以
   `INTEGER_OUT_OF_RANGE` 明确拒绝（一元负号是独立 AST 节点）。该最小值仍可
   经列值或 `(-9223372036854775807 - 1)` 在求值期构造，所有溢出路径照常检测。
   该行为在 README“已知限制”与 `OverflowTest` 中显式说明/锁定。

---

## 5. 当前未通过项 / 未覆盖项

- 自动化测试：**无未通过项（474/474 通过）**。
- 未实现（超出本次需求，刻意不做）：浮点数类型、`LIKE/BETWEEN/IN`、聚合、
  多表 JOIN、UPDATE/DDL、网络 HTTP 服务端与一切前端。JSON 入口设计为
  “请求文本 → 响应文本”，需要网络层时可直接包一层，不影响核心引擎。
- 已知语义取舍见 README 第 10 节（非结合比较、布尔不参与排序比较、
  字符串字典序、`-MIN` 字面量在编译期拒绝等）。
