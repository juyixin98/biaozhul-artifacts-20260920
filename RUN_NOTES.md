# 运行记录（实测，非推测）

- 日期：2026-09-23
- 系统：Linux 6.8.0-90-generic (x86_64, Ubuntu 24.04)
- JDK：环境初始无 JDK，下载免安装 Temurin 至 `~/.local/jdk-17`
  - `openjdk version "17.0.20.1" 2026-08-18 (Temurin-17.0.20.1+1)`，`javac 17.0.20.1`
  - 来源：`https://api.adoptium.net/v3/binary/latest/17/ga/linux/x64/jdk/hotspot/normal/eclipse`
- 第三方依赖：**0**（编译期、运行期、测试期均无外部 jar / 无 Maven / 无 Gradle）

## 1. 编译

命令：`JAVA_HOME=~/.local/jdk-17 ./build.sh`
结果：成功，无 warning 级以上输出；产物在 `out/classes`、`out/test-classes`。

## 2. 自动化测试

命令：`JAVA_HOME=~/.local/jdk-17 ./test.sh`
退出码：**0**

| 套件 | 结果 |
| --- | --- |
| 三值逻辑穷举：TRUE/FALSE/UNKNOWN 全组合 × 逐行 vs 向量 | 7/7 通过 |
| 类型检查 / 参数绑定 / 拒绝隐式混转 | 19/19 通过 |
| 空批次 / 跨批次 / 随机逐行对照 | 5/5 通过 |
| SQL 解析器 / 括号优先级 | 15/15 通过 |
| HTTP 端到端（JDK HttpServer + HttpClient） | 5/5 通过 |
| **合计** | **51/51 通过** |

关键验收点落实情况：

- **枚举 TRUE/FALSE/UNKNOWN 组合**：NOT 3 种、AND/OR 各 9 种、三原子混合 27 种
  及其括号变体、NOT/德摩根 9 种，共 75 个逻辑组合，期望值来自测试内独立书写的
  Kleene 真值表（非产品代码）。
- **逐行解释器对照向量结果**：上述每个组合都同时调用 `RowInterpreter` 与
  `VectorExecutor` 并断言相等；另有 40 组随机批次（固定随机种子 20260923，
  合计 >500 行、7 种表达式、随机注入 NULL）逐行对照。
- **空批次**：0 行批次（有 WHERE / 无 WHERE）计数与结果数组均正确。
- **跨批次**：同一编译计划连续跑空批次 + 两个非空批次，结果互不污染、
  累计计数正确；批次顺序交换后各自结果一致。
- **参数类型错误**：FLOAT 绑定 JSON 字符串、INTEGER 绑定小数、TEXT 绑定数字、
  参数数量不符等均返回 400 SEMANTIC_ERROR。
- **括号优先级**：AST 结构断言 `a OR b AND c` 解析为 `a OR (b AND c)`；
  `(a OR b) AND c` 27 组合中存在括号改变结果的组合，防止"假通过"。

## 3. HTTP 示例实测（PORT=8080）

### GET /health
```json
{
  "status": "ok"
}
```

### POST /query，examples/basic.json（两批次，含 NULL 行与空外批次场景）
- 批次1（3 行）：TRUE=1 / FALSE=1 / UNKNOWN=1，仅选中 `[1,"alice"]`；
  `tvl = ["TRUE","UNKNOWN","FALSE"]`
  - 第2行 bob：`score` 为 NULL → `>= ?` 为 UNKNOWN → 排除；
  - 第3行 name 为 NULL 且 7.0>=8 为 FALSE：UNKNOWN AND FALSE = FALSE → 排除。
- 批次2（2 行）：dave 6.0>=8 FALSE；erin 8.25>=8 且名字非空 TRUE → 选中 `[5,"erin"]`。
- `totalSelected = 2`。

### POST /query，examples/parentheses.json
SQL：`NOT ((a = ? OR b < ?) AND c <> ?)`，参数 1 / 2.5 / "x"。

| 行 | 手工推演 | 结果 |
| --- | --- | --- |
| [1,1.0,"x"] | (TRUE OR …)=TRUE，x<>'x'=FALSE，AND=FALSE，NOT=TRUE | TRUE（选中） |
| [null,3.0,"y"] | a=1 为 UNKNOWN，3.0<2.5 FALSE → OR UNKNOWN；y<>'x' TRUE → AND UNKNOWN → NOT UNKNOWN | UNKNOWN |
| [2,null,null] | FALSE OR UNKNOWN=UNKNOWN；c<>'x' 为 UNKNOWN → AND UNKNOWN → NOT UNKNOWN | UNKNOWN |

实测 `trueRows=1, unknownRows=2, selected=1`，与推演一致。

### POST /query，examples/bad-param-type.json → HTTP 400
```json
{
  "ok": false,
  "errorKind": "SEMANTIC_ERROR",
  "error": "params[0] 声明为 FLOAT，但收到字符串（拒绝隐式字符串转数值）"
}
```

### POST /query，examples/bad-type-mix.json → HTTP 400
```json
{
  "ok": false,
  "errorKind": "SEMANTIC_ERROR",
  "error": "类型不匹配：运算符 > 两侧为 TEXT 与 INTEGER（拒绝字符串与数值之间的隐式转换）"
}
```

## 4. 未完成项 / 已知边界（如实列出）

- 表名只做语法消费，不校验存在性，无目录/存储层。
- 不支持 JOIN、聚合、GROUP BY/ORDER BY、算术、IN/LIKE/BETWEEN、布尔列/布尔字面量；
  不支持列与列之外的复杂投影表达式。
- 服务为单进程内存执行：无持久化、无鉴权、固定 8 个守护工作线程、
  无速率限制；未做大规模批次的基准压测。
- JSON 解析器为自写最小实现，仅覆盖接口所需，不支持注释等非标准扩展。
- 未引入外部 SQL 引擎：全部核心逻辑（解析、类型、三值、向量执行）均在本仓库内，
  这一点由测试套件与代码结构直接保证。
