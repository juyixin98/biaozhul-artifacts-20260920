# ScL — 作用域与闭包转换工具链（纯后端）

一个从零实现的小语言（**S**cope/**c**losure **L**anguage）工具链：
手写词法分析、语法分析、**词法作用域分析**与**闭包转换**，把带嵌套函数、
可变局部量和词法作用域的源程序，变换成显式环境 / 单元（Cell）的栈式 IR，
再由一个**完全独立的解释器（VM）**执行。另有一个只依赖 Python 标准库的
JSON HTTP 服务。

核心验收点——闭包捕获的是**共享的可变单元**，而不是值的错误拷贝——由一个
源码树遍历解释器作为“对照预言机”，与转换后 VM 的输出逐条比对，覆盖遮蔽、
递归、逃逸闭包和多个闭包共享更新等场景。

> 无前端。所有核心解析与分析均为本项目手写，**没有**使用任何现成编译器、
> 解析器生成器或第三方依赖（仅 Python 3.10+ 标准库）。

---

## 目录结构

```
sclang/
  errors.py            源码位置 Span + 编译期/运行期错误
  lexer.py             手写词法分析器（每个 token 带 Span）
  ast_nodes.py         AST 节点定义（全部保留 Span）
  parser.py            手写递归下降解析器
  resolver.py          词法作用域分析：绑定、自由变量、装箱判定
  interpreter.py       源码树遍历参考解释器（对照预言机）
  ir.py                闭包转换后的显式环境/单元 栈式 IR（可 JSON 序列化）
  closureconvert.py    AST + 分析结果 -> IR 的闭包转换
  vm.py                独立栈式 VM（只读 IR，不接触 AST/解析器）
  pipeline.py          高层入口与 JSON 视图
  service.py           标准库 JSON HTTP 服务
  cli.py               命令行工具
examples/
  *.scl                示例源程序
  requests/*.json      HTTP 请求样例
  curl_demo.sh         一键演示脚本
tests/                 自动化测试（unittest，75 个用例）
```

---

## 语言定义（ScL）

### 词法

* 整数：`0`、`42`（任意精度整数，由宿主 int 支持）。
* 字符串：`"..."`，支持转义 `\n \t \r \" \\ \0`。
* 布尔：`true`、`false`；空值：`nil`。
* 标识符：字母/下划线开头，后接字母数字下划线。
* 注释：`#` 到行尾。
* 关键字：`let fn if else while return true false nil print and or not`。
* 运算符与标点：
  `+ - * / %`、`== != < <= > >=`、`=`、`( ) { } , ;`。

### 语法（EBNF 风格）

```
program   := stmt*
block     := "{" stmt* "}"
stmt      := let | fn | if | while | return | block | exprStmt
let       := "let" IDENT ("=" expr)? ";"
fn        := "fn" IDENT "(" params? ")" block
if        := "if" expr block ("else" block)?
while     := "while" expr block
return    := "return" expr? ";"
exprStmt  := expr ";"
params    := IDENT ("," IDENT)*

expr      := or
or        := and ("or" and)*                 # 短路
and       := equality ("and" equality)*      # 短路
equality  := comparison (("==" | "!=") comparison)*
comparison:= additive (("<"|"<="|">"|">=") additive)*
additive  := factor (("+" | "-") factor)*
factor    := unary (("*" | "/" | "%") unary)*
unary     := ("-" | "not") unary | call
call      := primary ("(" args? ")")*
primary   := INT | STRING | "true" | "false" | "nil"
           | IDENT ("=" expr)?                # 仅裸标识符可赋值
           | "(" expr ")"
           | "fn" "(" params? ")" block        # 匿名函数（lambda）
           | "print" "(" args? ")"
args      := expr ("," expr)*
```

### 语义

* **词法作用域 + 遮蔽**：名字绑定到最近的声明；`{ ... }` 引入新作用域，
  参数作用域与函数体块作用域也是两层。嵌套块中同名 `let` 会遮蔽外层，
  出块后恢复。同一作用域内重复声明是编译错误。
* **嵌套函数**：支持具名函数声明与匿名函数表达式。
* **函数提升（hoisting）**：块内的具名函数声明在该块执行前就可见，因此
  可前向调用并支持直接 / 相互递归。
* **可变局部量**：`let` 变量与参数都可重新赋值。
* **闭包**：函数捕获其定义处词法环境中的绑定；闭包逃逸后仍持有这些绑定。
* **整数运算**：`/`、`%` 向零截断（余数满足 `a = (a/b)*b + a%b`）。
  `bool` 与 `int` 是不同类型（`1 == true` 为 `false`）。
* `and`/`or` 短路并返回操作数原值（类似 Lua/JS），`not` 返回布尔。
* 条件按真值判断：`false` 与 `nil` 为假，非零整数为真。
* `print(...)` 以逗号分隔输出一行，返回 `nil`。

---

## 闭包转换是怎么做的（核心）

### 1. 词法作用域分析（`resolver.py`，不执行程序）

对每个函数（顶层程序是 0 号函数）计算：

* 每条声明（`let` / 参数 / 具名函数）的 **Binding**，在其属主函数帧内
  分配唯一局部槽位 `slot`；
* 每个函数的**自由变量有序列表** `free`：在本函数内使用、却定义在外层
  函数的绑定。捕获会**传递**——若 `inner` 用了 `outer` 的变量而 `middle`
  夹在中间，则 `middle` 也会转发该绑定，即使它从不提到这个名字；
* 每个绑定是否 **captured**（被嵌套函数捕获）与 **mutated**（被赋值）。

**函数身份与词法深度分离**：每个函数有唯一 `func_id`（发现顺序）和词法
`depth`（嵌套层级，兄弟函数 depth 相同）。绑定属主用 `func_id`，捕获传播
按 `depth` 沿词法链处理——这保证同层多个兄弟闭包不会被混淆。

### 2. 装箱判定（Cell boxing）

```
boxed  ⇔  具名函数绑定   ∨   被某个嵌套函数捕获的局部量/参数
```

* 被捕获的绑定放进一个堆上 **Cell（单值容器）**，在属主块的任何语句执行
  **之前**预创建；闭包在块入口的提升阶段创建闭包对象时，捕获的是这个
  **稳定的 Cell 本身**（按身份），而不是当时的值。
* 因此：闭包写入会反映到属主帧与所有兄弟闭包（**共享更新**）；提升闭包
  也不会在 `let` 初始化前快照到 `nil`。
* **未被捕获**的可变局部量仍是普通槽位（`SET_LOCAL`），这是保留的精确
  装箱优化——不逃逸就不装箱。`mutated` 位仍单独记录并在分析结果中暴露。

### 3. 显式环境 IR（`ir.py`）

嵌套被“拍平”为函数记录：每个函数有扁平局部槽与一个环境向量。关键指令：

| 指令 | 含义 |
|---|---|
| `MAKE_CELL slot` | 把槽位包装成 Cell（帧 prologue / 装箱参数） |
| `LOAD_CELL / STORE_CELL slot` | 经本帧槽位里的 Cell 读 / 写 |
| `PUSH_FREE / READ_FREE / WRITE_FREE i` | 访问环境向量第 i 项 |
| `MAKE_CLOSURE fnid k <(kind,idx)×k>` | 构造闭包；逐项从**当前帧局部槽** `(0,slot)` 或**当前函数自己的环境** `(1,i)` 取**原始存储**组装环境向量，Cell 身份沿词法链透传 |
| `CALL / RET / JMP / JIF_FALSE` 及算术比较 | 栈式调用、控制流、运算 |

IR 是纯数据（整数指令 + 常量表 + 每条指令的 6 元组源码位置），可直接
`to_dict()` / `from_dict()` 做 JSON 往返。

### 4. 独立 VM（`vm.py`）

VM **只消费 IR**，不 import AST、parser 或 resolver，因此它不可能“偷用”
源码层作用域逻辑。每帧持有 `slots` 与 `env`（其中 boxed 项是共享 Cell）；
`MAKE_CLOSURE` 按身份取槽 / 取环境项，多个闭包因此引用同一个 Cell。

---

## 快速开始

需要 Python 3.10+（开发与测试于 3.12）。无第三方依赖。

```bash
# 运行转换后程序（走闭包转换 + 独立 VM）
python3 -m sclang.cli run examples/counter.scl

# 运行源码参考解释器
python3 -m sclang.cli ref examples/counter.scl

# 对照：两侧输出必须一致
python3 -m sclang.cli diff examples/counter.scl

# 查看词法分析结果（绑定/自由变量/装箱）
python3 -m sclang.cli check examples/counter.scl

# 打印闭包转换后的栈式 IR
python3 -m sclang.cli compile examples/bank.scl
```

`counter.scl` 的对照输出（两个工厂实例互不干扰，同一实例内 inc/get 共享）：

```
reference: ['1', '2', '3', '1', '4']
vm       : ['1', '2', '3', '1', '4']
MATCH
```

## JSON HTTP 服务

```bash
python3 -m sclang.service --host 127.0.0.1 --port 8000
```

| 方法/路径 | 请求 | 说明 |
|---|---|---|
| `GET /healthz` | — | 健康检查 |
| `POST /lex` | `{source}` | token 流（含 span） |
| `POST /parse` | `{source}` | AST 摘要（含 span） |
| `POST /analyze` | `{source}` | 绑定、自由变量、捕获/装箱报告 |
| `POST /compile` | `{source}` | 闭包转换后的 IR 模块 + 反汇编文本 |
| `POST /eval` | `{source}` | VM 与参考解释器各跑一遍并比对 |

编译错误返回 **400**，运行期错误返回 **422**，均带结构化 `span` 与源码
`snippet`。请求体样例见 `examples/requests/*.json`。一键演示：

```bash
./examples/curl_demo.sh 8765
```

`/eval` 响应（节选）：

```json
{
  "ok": true,
  "output": ["1", "2", "1", "3"],
  "reference_output": ["1", "2", "1", "3"],
  "outputs_match": true,
  "results_match": true
}
```

未绑定变量的错误响应（位置贯穿到 token/字节行列）：

```json
{
  "ok": false,
  "stage": "compile",
  "error": "unbound variable 'x'",
  "span": {"line": 1, "col": 7, "end_line": 1, "end_col": 8, ...},
  "snippet": "print(x);\n      ^"
}
```

---

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试分层：

* `test_lexer_parser.py` — 词法/语法、优先级、结合性、位置、错误；
* `test_resolver.py` — 遮蔽、自由变量、传递捕获、装箱判定、槽位、
  兄弟函数唯一身份；
* `test_differential.py` — **验收对照**：同一程序在参考解释器与转换后 VM
  上输出必须相同，覆盖遮蔽、直接/相互递归、逃逸闭包、多个闭包共享更新、
  工厂实例隔离、传递捕获、短路、整数除法/取余等；
* `test_ir_vm.py` — IR 结构、唯一函数 id、装箱 vs 普通槽代码生成、
  JSON 往返、**Cell 身份在多个闭包间共享**、VM 运行期错误；
* `test_service.py` — 启动真实 HTTP 服务测试各端点、状态码、错误位置。

除固定用例外，开发期间还运行过数千个随机表达式程序与结构化闭包/递归
程序做差异 fuzz（两侧输出与“是否报错”均一致）。

### 关键不变量（被测试钉住）

1. **共享而非拷贝**：同一工厂内多个闭包持有的是同一个 Cell 对象
   （`assertIs(cells[0], cells[1])`），一个写入，其余可见。
2. **实例隔离**：两次工厂调用产生两个独立 Cell，互不影响。
3. **逃逸有效**：闭包返回 / 存入外部变量后，仍能读写其创建处的状态。
4. **遮蔽精确**：不同块中的同名 `let` 是不同 Binding，闭包绑定定义处那一个。
5. **转换保真**：转换后 VM 与源码参考解释器在所有用例上输出逐行一致。
