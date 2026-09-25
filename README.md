# taintflow — 小语言跨函数污点分析工具链与 JSON 服务

`taintflow` 是一个**纯后端**的教学/研究性质污点分析系统。它包含一套**自定义
小语言**、一条**完全手写**的工具链（词法 → 语法 → 自研 IR），以及一个在自研
IR 上做**过程间污点分析**的分析器，最后以**库 API** 和**纯 JSON HTTP 服务**
两种方式对外提供能力。

- 核心解析（词法/语法）**不依赖任何现成编译器或解析器生成器**，全部手写。
- 核心分析（污点传播、摘要、不动点）**不依赖任何第三方库**，仅用 Python 标准库。
- 从 token 到 AST 到 IR 指令，全链路**保留源码位置**（文件名:行:列）。
- 输出每条告警的一条**从 source 到 sink 的传播路径**，逐步可回溯到行列。
- **不宣称对任意语言完备**：分析对象只是下文定义的这门小语言，且分析本身是
  有意的保守过近似，已知误报会被显式标注（见“保守近似与已知误报”）。

---

## 目录

- [快速开始](#快速开始)
- [小语言定义](#小语言定义)
- [自研 IR](#自研-ir)
- [分析模型与算法](#分析模型与算法)
- [保守近似与已知误报](#保守近似与已知误报)
- [JSON 服务](#json-服务)
- [库 API](#库-api)
- [请求/响应样例](#请求响应样例)
- [测试与运行记录](#测试与运行记录)
- [目录结构](#目录结构)
- [非目标与完备性声明](#非目标与完备性声明)

---

## 快速开始

环境：Python 3.10+（开发与验收使用 Python 3.12），无第三方依赖，无需安装。

```bash
# 命令行分析一个源文件（人类可读报告）
python3 -m taintflow.cli analyze examples/01_simple.tfl

# 输出与 HTTP 服务一致的 JSON
python3 -m taintflow.cli analyze examples/05_recursion.tfl --json

# 附带自研 IR
python3 -m taintflow.cli analyze examples/01_simple.tfl --ir

# 启动 JSON HTTP 服务
python3 -m taintflow.cli serve --host 127.0.0.1 --port 8000
```

`examples/` 目录按编号给出了各关键场景的 `.tfl` 源码。

---

## 小语言定义

完整 EBNF 见 [`docs/LANGUAGE.md`](docs/LANGUAGE.md)。这里给出概览。

### 程序结构

程序由一个或多个函数定义组成（无类、无模块、无导入）：

```
program := fndef+
fndef   := 'fn' IDENT '(' params? ')' block
params  := IDENT (',' IDENT)*
block   := '{' stmt* '}'
```

函数只有直接调用 `f(args...)`，**没有**函数值、方法调用、闭包或间接调用。
入口约定为 `main`；若程序中没有 `main`，分析器会以“全干净参数”分析所有
函数（仍能发现某函数内部的 `source -> sink`）。

### 语句

```
stmt := ';'
      | 'return' expr? ';'
      | 'if' '(' expr ')' block ('else' (block | ifstmt))?
      | 'while' '(' expr ')' block
      | expr ';'                      # 表达式语句
      | IDENT '=' expr ';'            # 赋值（仅简单变量）
```

- 变量无需声明，**首次赋值即定义**；函数作用域（无块级作用域、无嵌套函数）。
- `else` 按最近嵌套结合（dangling-else 的标准约定）。
- 函数末尾无 `return` 时等价于返回空值。

### 表达式

字面量：整数 `42`、字符串 `"..."` / `'...'`、布尔 `true` / `false`。

运算符（优先级从低到高）：

| 层级 | 运算符 |
|---|---|
| 逻辑或 | `\|\|` 或 `or` |
| 逻辑与 | `&&` 或 `and` |
| 相等 | `==` `!=` |
| 关系 | `<` `>` `<=` `>=` |
| 加减 | `+` `-` |
| 乘除模 | `*` `/` `%` |
| 一元 | `!`（或 `not`）、`-` |
| 基本式 | 字面量、变量、`f(args)`、`(expr)` |

注释：`//` 行注释与 `/* ... */` 可嵌套块注释。

### 三个内置语义（污点相关）

| 内置 | 形态 | 语义 |
|---|---|---|
| **源 source** | `source()` | 无参；产生一个污点值 |
| **清洗器 sanitizer** | `clean(x)` | 一元；返回与入参无关的**干净**值（污点在此终止） |
| **汇 sink** | `sink(x)` | 一元语句型调用；污点到达即告警 |

三者名字都可在配置中替换为任意标识符集合（如 `getInput` / `escape` /
`writeLog`）。`clean` 是**返回新值**而非原地净化：`b = clean(a)` 后旧别名
`a` 仍带污点。

> 小语言是**故意裁剪**的：没有数组/对象/指针/字段/字符串索引，因此不存在
> “字段敏感/索引敏感”问题，也没有别名。这让分析的保守性来源清晰可控。

---

## 自研 IR

定义见 [`taintflow/ir.py`](taintflow/ir.py)。AST 被降级为**标签基本块 +
线性三地址指令**的控制流图（CFG），每条指令都带源码 `Span`。

指令集合：

| IR 指令 | 含义 |
|---|---|
| `Const dst` | 字面量 → 干净临时量 |
| `Copy dst src` | 变量/临时量复制 |
| `Binop dst op l r` | 二元运算（任一操作数脏则结果脏） |
| `Unop dst op x` | 一元运算 |
| `Source dst` | `dst = source()`，污点入口 |
| `Sanitize dst x` | `dst = clean(x)`，输出恒干净 |
| `Sink x` | `sink(x)`，汇 |
| `Call dst f args` | 用户函数 / 未知函数调用 |
| `Ret x?` | 返回 |
| `Jump L` / `Br cond Lthen Lelse` | 终结指令 |

`if` 降级为 `entry -(br)-> {then, else} -> join`；`while` 降级为
`head <-> body` 的带回边 CFG。可用 `--ir` 或请求 `"include_ir": true` 查看。

---

## 分析模型与算法

代码：[`taintflow/taint_model.py`](taintflow/taint_model.py)（过程内）与
[`taintflow/analyzer.py`](taintflow/analyzer.py)（过程间）。

### 污点格

每个程序点的抽象状态是 `变量 -> 污点标记集合`（幂集格，∪ 合并）。一个污点
标记 `Taint` 含两部分：

- `origin`：唯一定位**一次** `source()` 调用的三元组
  `(函数, 基本块, 指令序号)`。格的相等/连接只看 origin，因此格有限，不动点
  必然终止。
- `trace`：一条**符号路径轨迹**，仅供展示，不参与数据流的比较与合并。

### 符号轨迹与 β-归约（路径为何能跨函数连贯）

轨迹由片段组成：`SrcFrag`（根在被调函数内部 source）、`ParFrag(arg_index)`
（污点来自被调函数的第 `arg_index` 个形参）、`StepFrag`（一次具体传播，带
行列）。

函数摘要是**多态且符号化**的：形参的污点以 `ParFrag(i)` 表示，不绑定具体
调用者。在每个调用点，过程内转移对被调摘要做一次 **β-归约**：

```
ParFrag(i, origin, bind)  ──►  第 i 个实参在 caller 现场的轨迹 + bind + 后缀
```

实参轨迹自身可能仍以 `ParFrag` 开头（形参层层下传），于是 main→outer→inner
这样的嵌套会在**每层调用点各归约一次**，自然展开成：

```
source → 绑定outer形参 → 绑定inner形参 → inner返回 → outer返回 → sink
```

β-归约只替换一层（实参内部的 ParFrag 属于更外层函数，不能用当前环境再归约，
否则形参轨迹会在同一环境自我替换、无限增长）。这与 λ 演算按调用点逐次替换
实参的思想一致。

### 有限上下文摘要（刻意不全上下文敏感）

每个被分析的函数“实例”由二者区分：

1. **参数污点形状**：每个形参的 origin 集合。干净实参 / 污点实参 / 两个不同
   source 的实参分别建摘要（多态/polyvariant 划分）。
2. **k 限定调用串**：只保留最近 `context_k` 个调用点
   `(调用者:行号#序号)`（默认 `k=2`）。递归深度超过 k 后上下文被合并。

### 单调不动点

- 过程内：CFG 上混沌迭代，块入口 = 各前驱出口的 ∪，直到每个变量的 origin
  集合不再变化。
- 过程间：摘要从空起步，被调摘要增长（“new 中出现 old 没有的 origin”）时把
  依赖它的调用者重新入队。格有限 + 转移单调 ⇒ **必然终止**（递归/相互递归/
  循环都已验证，见测试）。

### 入口污点、赋值、分支、调用、清洗器

这五项正是题目要求的核心：

- **入口污点**：`Source` 指令产生带唯一 origin 与 SrcFrag 轨迹的标记。
- **赋值/传播**：`Copy`/`Binop`/`Unop` 把操作数污点的并集传到目标，轨迹追加
  一条带位置的传播步骤。
- **分支**：then/else 两支都分析，join 处取 ∪；不解释分支条件。
- **调用**：经过程间摘要 + 调用点 β-归约跨函数传播（见上）。
- **清洗器**：`Sanitize` 输出恒为空污点集，轨迹在此终止。

---

## 保守近似与已知误报

污点分析是“**may（可能）**”分析：宁可多报，不可漏报。下面三类过近似是
**有意的设计取舍**，系统会尽量在输出里标注，而不是把它们当作确定告警。

- **FP-1 未定义函数（外部调用）**：调用了程序中没有定义的函数时，默认保守地
  认为其返回值**可能携带任一污点实参**。这类告警标记为
  `possible_false_positive`，路径上带 `unknown_propagation` 步骤。可用
  `conservative_unknown_calls: false` 改为直接报错。
  例：[`examples/06_unknown_call.tfl`](examples/06_unknown_call.tfl)。

- **FP-2 分支/条件清洗（不做路径敏感）**：分析不做常量传播与分支条件约束
  求解，then/else 都视为可能执行。因此当“一支清洗、另一支不清洗”时，汇合后
  污点仍存活，即使实际运行只会走清洗那一支。系统用一遍 may/must 确认分析
  识别“污点只在部分路径存活”，标为 `possible_false_positive_branch`。
  例：[`examples/04_branch_clean.tfl`](examples/04_branch_clean.tfl)。
  循环的“0 次执行”同理：清洗只发生在循环体内时，0 次迭代路径未清洗，产生
  保守告警，见 [`examples/07_loop.tfl`](examples/07_loop.tfl)。

- **FP-3 k 限定上下文**：调用串只保留最近 `k` 个调用点。超过 k 层的递归共用
  同一上下文摘要，路径上下文信息被合并。这保证了终止性与摘要数量有界，代价
  是深层递归/多条递归路径的区分能力下降。调大 `context_k` 可缓解（成本更高）。

**分类字段**：

| `classification` | 含义 |
|---|---|
| `true_positive` | 在当前 may 分析下存在 source→sink 路径 |
| `possible_false_positive` | 经过未定义函数的保守传播（FP-1） |
| `possible_false_positive_branch` | 污点只在部分分支/循环路径存活（FP-2） |

此外，构建阶段还会给出与污点无关的 `warnings`（如变量可能未赋值即读取、
调用了未定义函数）。

---

## JSON 服务

```bash
python3 -m taintflow.cli serve --port 8000
```

- `GET /health`：存活检查。
- `POST /analyze`：分析一段源码。

请求体：

```json
{
  "source": "fn main(){ sink(source()); }",
  "entry": "main",
  "include_ir": false,
  "config": {
    "sources": ["source"],
    "sinks": ["sink"],
    "sanitizers": ["clean"],
    "context_k": 2,
    "max_trace": 40,
    "conservative_unknown_calls": true
  }
}
```

只有 `source` 必填。成功返回 HTTP 200，词法/语法/语义错误返回 HTTP 400，
响应统一为 `{"ok": false, "error": {"type", "message", "span"}}`，服务进程
不抛栈。

响应关键字段：

- `summary.alerts` / `true_positives` / `possible_false_positives`
- `summary.function_contexts`：建立的有限上下文摘要数量
- `summary.reanalyses` / `intra_iterations`：不动点工作量（可用于观察递归）
- `alerts[]`：每条含 `classification`、`source_point`（行列）、`sink`
  （行列）、`context`（k 限定调用串）、`path[]`（逐步 source→sink，每步带
  `function:line:col`）。

完整的请求/响应成品见
[`examples/request_basic.json`](examples/request_basic.json) 与
[`examples/response_basic.json`](examples/response_basic.json)。

---

## 库 API

```python
from taintflow import analyze_source, AnalysisConfig

cfg = AnalysisConfig.from_dict({"context_k": 3})
result = analyze_source("fn main(){ sink(source()); }", cfg)

assert result["ok"]
for a in result["alerts"]:
    print(a["source_point"]["line"], "->", a["sink"]["line"],
          a["classification"])
    for step in a["path"]:
        print("   ", step["at"], step["kind"], step["desc"])
```

`analyze_source(source, config=None, *, filename="<input>", entry="main",
include_ir=False)` 返回可直接 `json.dumps` 的字典；错误同样结构化（不抛
`TaintflowError`）。也可以直接组合底层模块：
`Lexer → Parser → IRBuilder → InterproceduralAnalyzer`。

---

## 请求/响应样例

```bash
python3 -m taintflow.cli serve --port 8000 &
curl -s -X POST http://127.0.0.1:8000/analyze \
     -H 'Content-Type: application/json' \
     --data @examples/request_basic.json
```

- [`examples/request_basic.json`](examples/request_basic.json) /
  [`examples/response_basic.json`](examples/response_basic.json)：
  跨函数 + 条件清洗（含一条 FP-2 可能误报）。
- [`examples/request_custom_names.json`](examples/request_custom_names.json) /
  [`examples/response_custom_names.json`](examples/response_custom_names.json)：
  自定义源/汇/清洗器名字。

---

## 测试与运行记录

运行全部自动化测试（标准库 `unittest`，无第三方依赖）：

```bash
python3 -m unittest discover -s tests -v
```

测试文件：

- `tests/test_lexer.py`：词法、位置、字符串转义、可嵌套注释、词法错误。
- `tests/test_parser.py`：AST、优先级、dangling-else、语句形式、语法错误。
- `tests/test_ir.py`：指令降级、CFG 边、内置识别、参数个数等语义检查。
- `tests/test_analyzer.py`：**核心验收**，覆盖
  直接传播、清洗、**不同调用上下文**、线性/相互/**树状递归**、
  **条件清洗（含已知误报）**、while 循环 0..n 次近似、未知函数 FP-1、
  路径连贯性与位置、无 main、多 source/多汇、错误结构化。

最近一次实测命令与结果（Python 3.12.3）：

```text
$ python3 -m unittest discover -s tests
Ran 83 tests in 0.04s
OK
```

各样例实测（告警数 / 确定 / 可能误报；`摘要` 为有限上下文函数实例数）：

| 样例 | 场景 | 结果 |
|---|---|---|
| 01_simple | 单层跨函数 | 1 告警，确定 |
| 02_context | 同函数干净/污点两种上下文 | 仅污点上下文 1 告警 |
| 03_sanitizer | 清洗阻断 + 未清洗对照 | 1 确定，2 处清洗后不报 |
| 04_branch_clean | 常量条件清洗（FP-2） | 1 可能误报 |
| 05_recursion | 线性递归 | 1 确定 |
| 06_unknown_call | 未定义函数（FP-1） | 1 可能误报 |
| 07_loop | while 传播 + 循环内清洗（0 次近似） | 1 确定 + 1 可能误报 |
| 08_mutual_recursion | 相互递归 | 1 确定 |
| 09_deep_recursion_k | 100 层深递归（k 限定，FP-3） | 1 确定，仅 4 个摘要即终止 |

终止性补充实测（`09`，深度 100，不同 k 均快速终止）：

```text
context_k=0 : 2 摘要, 3 轮重分析   # 无上下文
context_k=1 : 3 摘要, 4 轮重分析
context_k=2 : 4 摘要, 5 轮重分析   # 默认
```

> 这些数字由上面的命令在本机实际跑出；若修改了默认配置（如 `context_k`、
> 内置名），数字可能相应变化，属于预期。

---

## 目录结构

```
taintflow/
  location.py     源码位置 Pos/Span
  errors.py       Lex/Parse/Analysis 错误（带 span）
  config.py       源/汇/清洗器名与 k 等配置
  lexer.py        手写词法分析
  ast_nodes.py    AST 定义（全带 span）
  parser.py       手写递归下降语法分析
  ir.py           自研 IR（CFG + 三地址指令）与 AST->IR
  taint_model.py  污点格、符号轨迹/β-归约、过程内 may(+must) 分析
  analyzer.py     有限上下文摘要、过程间单调不动点、告警分类
  service.py      analyze_source 统一入口 + JSON HTTP 服务
  cli.py          命令行（analyze / serve）
examples/         .tfl 源码与 .json 请求/响应样例
tests/            自动化测试
docs/LANGUAGE.md  小语言 EBNF 与语义细则
```

---

## 非目标与完备性声明

- 本系统**只分析本仓库定义的小语言**（见 `docs/LANGUAGE.md`）。它不是也不
  试图成为 C/Java/Python/JS 等任意外部语言的完备分析器；**不宣称对任意
  语言完备**。
- 分析是 **may 过近似 + 有限上下文（k 限定调用串、无路径敏感、无字段/索引
  敏感）**。它保证在该模型与小语言语义下**不漏报** source→sink 的数据流，
  但会产生上文 FP-1/FP-2/FP-3 所述的、被显式标注的误报。
- 小语言刻意不含别名/堆/并发/异常/动态分派，因此相关 soundness 问题不在
  适用范围内。
- 无前端：交付物为库、CLI、JSON 服务、样例与测试，不含任何 UI。
