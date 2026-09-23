# Slang — 作用域与闭包转换（Scope & Closure Conversion）工具链

一个纯后端的小语言工具链库 + JSON 服务。它实现一门带**嵌套函数、可变局部量和词法作用域**的小语言
**Slang**，核心演示目标是：

> 闭包捕获的是**共享的可变单元（cell）**，而不是值的拷贝——多个闭包对同一被捕获变量的更新彼此可见，
> 逃逸出创建函数的闭包仍能读写该变量。

整个管线（词法、语法、作用域/逃逸分析、闭包转换、两套解释器）全部**手写**，只使用 Python 标准库，
**不依赖任何现成编译器、解析器生成器或第三方包**。

---

## 1. 目录结构

```
slang/
  errors.py        # 带源码位置的错误类型（lex/parse/compile/runtime）
  values.py        # 运行值、真值、二元/一元运算（两套解释器共享）
  lexer.py         # 手写词法分析器（逐字符扫描，token 全部带行列/偏移位置）
  ast_nodes.py     # AST 节点（每个节点都带 loc，可序列化为 JSON）
  parser.py        # 手写递归下降 + 优先级爬升
  analyzer.py      # 作用域分析 + 逃逸分析（决定哪些绑定装箱为 cell）
  ir_nodes.py      # 闭包转换后的显式 IR（结构化语句 + 栈机子表达式）
  lower.py         # 闭包转换器：AST + 分析结果 -> IR（lambda 提升 / cell / free 向量）
  ir_interp.py     # 独立的 IR 解释器（只执行 IR，完全不看 AST/分析器）
  source_interp.py # 源参照解释器（直接执行 AST，词法环境链，独立实现）
  pipeline.py      # 高层管线封装
  service.py       # JSON HTTP 服务（标准库 http.server）
  cli.py           # 命令行：lex/parse/analyze/lower/run/compare/serve
examples/          # 6 个覆盖验收点的 .slang 程序
requests/          # HTTP 请求样例（JSON）+ curl 脚本
tests/             # 自动化测试（unittest），含差分测试与随机差分测试
run_tests.sh       # 一键运行全部测试
requirements.txt   # 无第三方依赖（仅说明 Python 版本）
```

---

## 2. Slang 语言语法（本文档即语言规约）

### 2.1 词法

* 整数 `int`（任意精度）、字符串 `"..."` / `'...'`（支持 `\n \t \r \\ \" \' \0` 转义）、
  布尔 `true` `false`、空值 `null`。
* 标识符：字母/下划线开头，后跟字母数字下划线。
* 关键字：`let fn return if else while true false null`。
* 运算符（优先级从低到高）：
  `||` → `&&` → `== !=` → `< <= > >=` → `+ -` → `* / %`；一元 `- !`；括号与调用 `f(...)`。
* 分隔符：`( ) { } [ ] , ;`。赋值用语句 `name = expr;`（不是表达式）。
* 注释：行注释 `// ...`，可嵌套的块注释 `/* ... */`。

### 2.2 语法（EBNF 风格）

```
program   := stmt*
stmt      := ';'
           | block
           | 'let' ID ('=' expr)? ';'
           | 'fn' ID '(' params? ')' block        # 函数声明（名字提升到当前函数作用域）
           | 'return' expr? ';'
           | 'if' '(' expr ')' block ('else' block | 'else' if-stmt)?
           | 'while' '(' expr ')' block
           | ID '=' expr ';'                       # 赋值语句
           | expr ';'
block     := '{' stmt* '}'
params    := ID (',' ID)*
expr      := 字面量 | ID | 'fn' ID? '(' params? ')' block   # 匿名/命名函数表达式
           | unary | binary | '(' expr ')' | call
call      := expr '(' args? ')'   （后缀，可链式 f(1)(2)）
```

### 2.3 语义要点

* **词法作用域**：名字在定义处的静态环境中解析。
* **块作用域 + 遮蔽（shadowing）**：`let` 绑定在最近的 `{}` 块内可见；嵌套块可用同名 `let`
  遮蔽外层，对外层绑定无副作用。函数参数遮蔽外层同名量。同一块内重复 `let` 同名是编译错误。
* **函数声明提升**：`fn name(){}` 的名字在其所在函数体内处处可见（槽位预分配），但闭包本身在
  执行到该声明语句时才建立；在声明语句之前调用会触发“初始化前读取”运行期错误（TDZ 风格）。
  声明之后即可**直接递归**；两个声明都执行后可**相互递归**。
* **命名函数表达式**：`let f = fn self(n){ ... self(...) }` 中的 `self` 只在函数体内可见。
* **可变与捕获（本项目核心）**：当一个嵌套函数引用了外层函数的可变绑定时，该绑定被**装箱（box）**
  到堆上的 **cell**（单槽可变对象）。所有捕获它的闭包共享同一个 cell，于是更新互相可见；
  创建者返回后 cell 仍然存活（逃逸闭包）。未被捕获的普通局部量保持为普通槽位，不装箱。
* `let x;`（无初始化）的值为 `null`。
* 内置：`print(x)`，单参数，输出一行到“输出轨迹（trace）”并返回 `null`。
* 整数除法向零截断（`-17/5 == -3`）；除零、参数个数不符、调用非函数等都是带位置的运行期错误。

---

## 3. 闭包转换是怎么做的（核心设计）

分析阶段（`analyzer.py`）为每个函数建立一个 **frame**，并得到：

| 字段 | 含义 |
|---|---|
| `slots` | 该函数全部局部绑定（参数、let、提升的函数名），文本顺序分配槽号 |
| `boxed_slots` | 被嵌套函数捕获、必须放进堆 cell 的槽位 |
| `free` | 本函数体引用的、定义在外层 frame 的变量 |
| `captures` | 为构造本函数闭包而从父级接收的 cell（含跨越多层时的中转 capture） |

转换阶段（`lower.py`）产出的 IR 中**不再有符号名引用**，只有三种访问方式：

* `GET_LOCAL/SET_LOCAL`：当前活动记录的私有槽位；
* `NEW_CELL/BOX`、`CELL_DEREF/CELL_SET`：堆单元的创建/装箱/读/写；
* `GET_FREE`：通过闭包的**显式 free 向量**取到被捕获的 cell。

嵌套函数被 **lambda 提升** 为顶层 `IFunc`；父函数在创建闭包处把捕获到的 cell 压栈，
用 `MAKE_CLOSURE fid` 把它们绑定进新闭包的 free 向量。跨越中间函数时，中间函数的
`captures` 会把 cell **一路透传**下去（见 `examples/05_deep_capture.slang`）。

关键不变量：**一个被捕获变量在整个程序里对应同一个 cell 对象**。因此

```slang
let n = 0;
let inc = fn(){ n = n + 1; return n; };
let dec = fn(){ n = n - 1; return n; };
```

里 `inc`、`dec` 看到的是同一个 `n`。

**两套解释器刻意独立**：`source_interp.py` 用经典的词法环境链直接跑 AST（捕获判定也由它自己从源码
重新计算，不读分析器结果）；`ir_interp.py` 只执行转换后的 IR。二者共享的仅是 `values.py` 里的
值/运算定义。验收测试就是断言二者对同一程序产生**完全一致的输出轨迹**（以及一致的错误行为）。

---

## 4. 安装与使用

只需 Python 3.10+（开发环境 3.12），无第三方依赖：

```bash
python3 --version        # 3.10+
# 无需 pip install
```

### 4.1 命令行

```bash
python3 -m slang.cli lex      examples/04_shared.slang   # 打印 token（含位置）
python3 -m slang.cli parse    examples/01_shadow.slang   # 打印 AST JSON
python3 -m slang.cli analyze  examples/03_escape.slang   # 作用域/逃逸分析 JSON
python3 -m slang.cli lower    examples/05_deep_capture.slang   # 闭包转换后 IR JSON
python3 -m slang.cli run      examples/04_shared.slang   # 跑转换后的 IR（默认）
python3 -m slang.cli run      examples/04_shared.slang --source  # 跑源参照解释器
python3 -m slang.cli compare  examples/02_recursion.slang # 两套解释器对照
python3 -m slang.cli serve --host 127.0.0.1 --port 8080   # 启动 JSON 服务
```

`compare` 在两解释器输出不一致时以非零状态码退出。

### 4.2 JSON HTTP 服务

`POST` 请求体统一为 `{"source": "...", "filename": "可选名"}`，成功返回
`{"ok": true, "result": ...}`；任何阶段的错误返回 HTTP 400：
`{"ok": false, "error": {"phase": "...", "message": "...", "loc": {...}}}`（错误带源码位置）。

| 方法/路径 | 作用 |
|---|---|
| `GET /health` | 健康检查 |
| `POST /parse` | tokens + AST（全部带位置） |
| `POST /analyze` | AST + 作用域/逃逸分析（frames/slots/boxed/free/captures） |
| `POST /lower` | 分析结果 + 闭包转换后的 IR |
| `POST /run/ir` | 用独立 IR 解释器执行，返回 `trace` |
| `POST /run/source` | 用源参照解释器执行，返回 `trace` |
| `POST /compare` | 两者都跑，返回 `agree` 与两份 trace |

请求样例见 `requests/`，可直接演示：

```bash
python3 -m slang.cli serve --port 8791 &
bash requests/curl_examples.sh        # 默认连 127.0.0.1:8791，可用 PORT= 覆盖
```

作为库使用：

```python
from slang.parser import parse
from slang.analyzer import analyze
from slang.lower import lower_module
from slang.ir_interp import IRInterpreter
from slang.source_interp import SourceInterpreter

tree = parse("print(1 + 2);")
module = lower_module(tree, analyze(tree))
print(IRInterpreter(module).run())          # ['3']
print(SourceInterpreter(tree).run())        # ['3']
```

---

## 5. 验收点与样例对照

| 验收点 | 样例 | 两套解释器输出 |
|---|---|---|
| 遮蔽（块 + 参数） | `examples/01_shadow.slang` | `1, 2, 20, 1, 107, 1` |
| 直接递归 / 命名函数表达式 / 相互递归 | `examples/02_recursion.slang` | `120, 3628800, 55, 1, 1` |
| 逃逸闭包（创建帧已退出） | `examples/03_escape.slang` | `2, 4, 102, 6, 104` |
| 多个闭包共享更新 | `examples/04_shared.slang` | `1, 2, 1, 1, 15, 22` |
| 跨多层捕获透传 | `examples/05_deep_capture.slang` | `1001, 1002, 1003` |
| 循环 / 短路 / 字符串 / 运算 | `examples/06_loops_strings.slang` | 见文件 |

---

## 6. 测试

```bash
bash run_tests.sh                 # 或：python3 -m unittest discover -s tests -v
```

* `test_lexer_parser.py`：词法位置、注释、优先级、结合性、悬空 else、链式调用、错误位置。
* `test_analyzer_lower.py`：遮蔽分槽、捕获即装箱、未捕获不装箱、参数捕获、跨层 capture 透传、
  递归捕获自身名、命名函数表达式 self cell、lambda 提升、IR 可 JSON 序列化、常量去重。
* `test_differential.py`：**核心验收**——14+ 个程序在源解释器与 IR 解释器下逐行对照，
  覆盖遮蔽、递归（直接/自命名/相互）、逃逸闭包、多个闭包共享更新、深层捕获、高阶函数、短路；
  并对除零、参数个数、调用非函数等运行期错误做行为一致性对照。
* `test_fuzz.py`：随机生成的计数器/遮蔽/递归程序（110 个种子）做差分测试。
* `test_service.py`：在真实端口启动 HTTP 服务，覆盖全部端点、错误位置与 400/404。

---

## 7. 已记录的设计取舍

* 闭包捕获采用 **boxed cell（装箱/共享单元）** 模型而非“按值复制”，这正是题目要求的共享语义。
* 结构化控制流（`if/while/block`）保留在 IR 语句层，表达式则被压平为栈机指令，便于序列化、
  也便于独立解释器直接执行。
* 两套解释器有意重复实现“名字解析/捕获”，以保证转换前后是两套真正独立的语义实现。
