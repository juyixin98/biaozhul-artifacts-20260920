# resflow — 资源释放路径分析工具（纯后端）

`resflow` 是一个从零实现的小语言工具链：自定义语言（**resflow/1**）的
词法分析器、递归下降语法分析器、语义检查器、显式控制流图（CFG）构造器，
以及一个**路径敏感**的资源释放分析器。所有解析与分析逻辑均为手写，
**不依赖任何编译器框架或第三方库**（HTTP 服务也只用 Python 标准库）。

它显式建模 `acquire` / `release` / `return` / `throw` 四种控制流事件，
其中**异常边是 CFG 上的一类真实边并参与全部计算**，可检测：

* `DOUBLE_RELEASE` —— 重复释放
* `RESOURCE_LEAK` —— 路径结束（正常或异常退出）时资源仍持有（未释放）
* `USE_AFTER_RELEASE` —— 释放后使用
* 以及补充诊断：`RELEASE_NOT_HELD`、`USE_NOT_HELD`（警告）、
  `OVERWRITE_HELD`（持有时再次 acquire 导致旧资源泄漏）

每条结论都带有**可复现的控制流路径**：按固定顺序枚举的节点序列、
每一步的资源状态、触发的事件、走过的边（normal / exception，以及
`true` / `false` / `back` 标签）和精确源码位置（文件:行:列）。

---

## 1. 目录结构

```
resflow/                  核心库（全部手写，零第三方依赖）
  locations.py            源码位置：Position / Span，偏移→行列换算
  errors.py               错误层次（E-LEX / E-PARSE / E-SEM / E-ANALYSIS）
  lexer.py                手写词法分析器
  ast_nodes.py            AST 定义（每个节点带 Span）
  parser.py               手写递归下降语法分析器
  semantics.py            语义检查（函数表、实参数量、作用域、checked throw）
  cfg.py                  CFG 构造：normal / exception 边、try/catch、循环回边
  summaries.py            跨函数结局摘要（may_return / may_throw 不动点）
  analyzer.py             路径敏感分析与结果序列化（主入口 analyze_source）
  cli.py                  命令行（check / serve）
  server.py               JSON over HTTP 服务（标准库 http.server）
examples/                 8 个 .rf 示例，覆盖验收场景
samples/                  HTTP 请求样例（JSON 与 curl）
tests/                    39 个自动化测试（unittest）
```

## 2. 环境要求

* Python 3.10+（开发环境为 Python 3.12.3，仅用标准库）
* 无需安装任何依赖。

## 3. 快速开始

```bash
# 文本报告
python3 -m resflow.cli check examples/03_double_release_and_uaf.rf

# JSON（不含 CFG）
python3 -m resflow.cli check examples/07_exception_exit_leak.rf --format json

# JSON（含完整 CFG）
python3 -m resflow.cli check examples/06_exception_cleanup_ok.rf --format json --cfg

# 调整循环展开界（每个 while 最多走多少次回边，默认 2）
python3 -m resflow.cli check examples/04_loop_acquire.rf --loop-bound 3

# 启动 JSON HTTP 服务
python3 -m resflow.cli serve --host 127.0.0.1 --port 8080
```

退出码：`0` 无 error 级发现；`1` 存在 error 级发现；`2` 输入文件无法读取
或存在词法/语法/语义错误（错误打到 stderr）。

## 4. HTTP 服务

```bash
python3 -m resflow.cli serve --port 8080
curl -s http://127.0.0.1:8080/health
curl -s -X POST http://127.0.0.1:8080/analyze \
  -H 'Content-Type: application/json' \
  --data @samples/request_double_release.json
```

* `GET /health` → `{"status":"ok","language":"resflow/1"}`
* `POST /analyze`

  请求：

  | 字段 | 类型 | 必填 | 说明 |
  |---|---|---|---|
  | `source` | string | 是 | resflow 源码 |
  | `filename` | string | 否 | 位置信息中显示的文件名（默认 `<http>`） |
  | `loop_bound` | int | 否 | 循环回边上界（默认 2） |
  | `max_steps` | int | 否 | 单条路径步数上限（默认 10000） |

  响应 `200`：完整分析结果（见第 7 节）。
  响应 `400`：`{"error":{"code","message","span"}}`，
  `code` 为 `E-LEX` / `E-PARSE` / `E-SEM` / `E-REQUEST`。

更多请求样例见 `samples/`，可直接运行 `bash samples/curl_examples.sh`。

---

## 5. resflow/1 语言规范（自定义，本文档即规范）

### 5.1 词法

* 注释：`//` 到行尾。
* 标识符：`[A-Za-z_][A-Za-z0-9_]*`。
* 整数字面量：`[0-9]+`；字符串：`"..."`，支持 `\n \t \" \\` 转义。
* 布尔字面量 `true` / `false`，空值 `null`。
* 关键字：
  `fun let acquire release use return throw if else while try catch throws true false null`
* 标点/运算符：`( ) { } , ; = ! && ||`
* 空白（空格、制表符、换行）仅起分隔作用。

### 5.2 语法（EBNF，`[]?` 可选、`{}*` 重复）

```
program     := { function }*                      (* 至少一个函数 *)
function    := "fun" IDENT "(" [params] ")" ["throws"] block
params      := IDENT { "," IDENT }*
block       := "{" { statement }* "}"

statement   := "let" IDENT "=" acquireCall ";"
             | "let" IDENT "=" call ";"
             | call ";"
             | "release" IDENT ";"
             | "use" "(" IDENT ")" ";"
             | "return" [expr] ";"
             | "throw" STRING ";"
             | "if" "(" expr ")" block ["else" (block | ifStatement)]
             | "while" "(" expr ")" block
             | "try" block "catch" "(" IDENT ")" block

acquireCall := "acquire" "(" STRING ")"
call        := IDENT "(" [ expr { "," expr }* ] ")"
expr        := orExpr
orExpr      := andExpr { "||" andExpr }*
andExpr     := unaryExpr { "&&" unaryExpr }*
unaryExpr   := "!" unaryExpr | primary
primary     := INT | STRING | "true" | "false" | "null"
             | IDENT | "(" expr ")"
```

刻意的简化（在语义检查中强制执行，违反报 `E-SEM`）：

1. **函数调用只能作为完整语句**（`f(...);` 或 `let x = f(...);`），
   不能出现在条件、实参或 return 表达式中——这样调用的异常边语义明确。
2. 条件表达式是**不透明布尔表达式**（参数或 `let x=f()` 的变量、
   字面量、`!`、`&&`、`||`），分析器不求值，两个分支都探索。
3. 变量为**函数作用域**（类似提升的 `let`）；catch 的错误变量仅在其
   catch 块内可见。
4. **受检异常**：`throw` 或调用 `throws` 函数，必须位于声明了 `throws`
   的函数内，或处于某个 `try` 体中（由其 catch 处理）。catch 体内抛出
   的异常不受该 try 自身捕获，会继续向外传播。

### 5.3 资源语义

* `let x = acquire("kind");` 将 `x` 置为 **HELD**。
* `release x;` 要求 `x` 为 HELD，置为 **RELEASED**；否则按路径状态报
  `DOUBLE_RELEASE`（已释放）或 `RELEASE_NOT_HELD`（从未获取，警告）。
* `use(x);` 要求 `x` 为 HELD；已释放报 `USE_AFTER_RELEASE`，
  从未获取报 `USE_NOT_HELD`（警告）。
* 对 HELD 变量再次 `acquire`：旧资源泄漏（`OVERWRITE_HELD` +
  `RESOURCE_LEAK`），变量重新 HELD。对 RELEASED 变量再次 acquire 是
  合法的重新获取（事件记为 `reacquire`）。
* 每条路径到达函数的**正常出口**（`return` 或末尾落入出口）或
  **异常出口**（未捕获异常）时，仍为 HELD 的资源报 `RESOURCE_LEAK`；
  泄漏信息会区分两种出口。

### 5.4 控制流与异常边语义（核心）

CFG 为每个函数生成两个合成终节点：`exit`（正常）与 `except_exit`（异常）。

* 普通语句之间是 `normal` 边。
* `if` 条件节点出两条 normal 边，标签 `true` / `false`；无 else 时
  `false` 直接汇入 then 分支的后继。
* `while`：条件→循环体标 `true`；循环体末尾→条件为回边，标 `back`；
  `false` 离开循环。
* `throw` 节点没有 normal 后继，只有一条 `exception` 边。
* 调用 `throws` 函数的 `call` 节点有两条边：normal（被调用方正常返回）
  与 exception（标签 `threw`）；调用普通函数只有 normal 边。
* `try { B } catch (e) { C }`：B 内任何 throw/call 的 exception 边指向
  该 try 的 catch 头；catch 头→C 标 `entered-catch`。C 内新抛出的异常
  按外层上下文解析（外层 catch 或函数 except_exit）。
* exception 边在**构造时**按词法嵌套的最近 try 解析，因此嵌套 try、
  循环内抛出、catch 内再抛出都精确建模。

跨函数精度：在调用图上以最小不动点计算每个函数的结局摘要
`may_return` / `may_throw`（见 `summaries.py`）。必抛函数
（如 `fun f() throws { throw "x"; }`）`may_return=false`，其调用点
不再产生虚假的“正常返回”后继，因此调用点之后的死代码不会出现在任何
路径上。

### 5.5 循环与可复现性

条件不求值，循环按界展开：`loop_bound` 是同一个 while 头允许经过的
`back` 边次数（默认 2，即探索 0、1、2 次执行循环体）。展开界耗尽时
仍可继续的路径记录为终态 `divergent`（表示“界外行为未展开”，
**不是**错误）。路径枚举顺序完全确定：节点 id 按构造顺序，出边按
（边类别、`false`→`true`→`back`、目标 id）排序；同一份源码重复分析
产生字节一致的 JSON（测试 `test_results_byte_stable_across_runs`）。

---

## 6. 分析如何覆盖验收场景

| 场景 | 示例 | 预期 |
|---|---|---|
| 分支部分初始化 | `examples/02_partial_init.rf` | false 路径 USE_NOT_HELD + RELEASE_NOT_HELD；true 路径干净 |
| 循环获取 | `examples/04_loop_acquire.rf` | 0/1/2 次迭代均平衡，额外 1 条 divergent 截断路径 |
| 循环中泄漏/覆盖 | `examples/05_loop_reacquire_leak.rf` | RESOURCE_LEAK（出口持有）+ OVERWRITE_HELD（回边后重取） |
| 异常退出 | `examples/07_exception_exit_leak.rf` | exception 路径到 except_exit 时仍持有 → RESOURCE_LEAK |
| 异常边参与计算、catch 清理 | `examples/06_exception_cleanup_ok.rf` | fail() 的异常边进 catch 释放，唯一路径干净 |
| 重复释放 / 释放后使用 | `examples/03_double_release_and_uaf.rf` | 两分支分别报 DOUBLE_RELEASE、USE_AFTER_RELEASE |
| 嵌套 try 与 catch 再抛 | `examples/08_nested_try_rethrow.rf` | 内层 catch 释放后 re-throw，外层 catch 接住，无泄漏 |
| 不可达/必抛精确性 | `examples/06`、`07` | may_return=false，调用点后的死代码不出现在任何路径 |

## 7. JSON 输出结构

```jsonc
{
  "language": "resflow/1",
  "filename": "x.rf",
  "options": {"loop_bound": 2, "max_steps": 10000},
  "summaries": [{"name": "main", "throws": false,
                 "may_return": true, "may_throw": false}],
  "functions": [{
    "name": "main", "params": [], "throws": false,
    "span": { ... },
    "resource_variables": ["f"],
    "cfg": {
      "entry": "nentry_1", "exit": "nexit_2", "except_exit": "...",
      "nodes": [{"id", "kind", "label", "span"}],
      "edges": [{"src", "dst", "kind": "normal|exception", "label"}]
    },
    "paths": [{
      "path_id": 1,
      "kind": "normal | exception | divergent",
      "node_sequence": ["nentry_1", ...],
      "steps": [{
        "node_id", "kind", "label",
        "span": {"filename", "start": {"offset","line","column"}, "end": {...}},
        "via_edge": {"src","dst","kind","label"} | null,
        "state_after": {"f": "ABSENT | HELD | RELEASED"},
        "events": [{"type": "acquire|release|use|reacquire|finding", ...}]
      }],
      "finding_ids": [1]
    }],
    "finding_ids": [1]
  }],
  "findings": [{
    "id": 1, "code": "DOUBLE_RELEASE", "message": "...",
    "function": "main", "variable": "f", "resource_kind": null,
    "node_id": "nrelease_7",
    "span": {"filename", "start": {"offset","line","column"}, "end": {...}}
  }],
  "counts": {"functions": 2, "paths": 3, "findings": 2, "truncated_paths": 0}
}
```

位置信息贯穿三层：词法 token、AST 节点、CFG 节点与 finding 都携带
`Span`（文件 + 起止字符偏移与行列）。

## 8. 运行测试

```bash
python3 -m unittest discover -s tests -t .
# 或
python3 tests/run_tests.py
```

测试覆盖：词法/语法错误与行列位置、语义规则（重名函数、未声明调用、
实参数量、未定义变量、受检异常、调用位置限制）、三类核心缺陷、
分支部分初始化、循环展开与界外截断、异常边进入 catch、嵌套 re-throw、
跨函数摘要、递归发散、结果可复现性，以及 HTTP 服务的 8 个端到端用例。

## 9. 设计说明与局限

* 分析是**有界路径枚举**（不是抽象解释的合并状态），换来了可直接阅读、
  可复现的逐步路径与状态；代价是路径数随分支/循环界指数增长，
  因此提供 `loop_bound` 与 `max_steps` 两个显式上界。`divergent`
  终态如实标注“未展开”，不假装分析过界外行为。
* 不求值条件：同一变量在不同分支的获取状态靠路径分叉区分（路径敏感），
  但不会根据常量条件剪枝不可能分支。
* 不做跨过程的资源别名追踪：资源只通过本地变量标识；调用不转移资源
  所有权（小语言刻意保持的边界）。
