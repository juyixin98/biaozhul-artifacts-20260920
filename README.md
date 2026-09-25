# MiniML — 带 let 多态的函数式语言类型推导工具链（纯后端）

一个**从零实现**（手写词法分析、手写语法分析、手写类型推导，不依赖任何现成
编译器/前端框架）的小型函数式语言 **MiniML** 工具链，包含：

- 保留**源码位置**（行列号）的 lexer / parser；
- 基于 **Algorithm W（Damas–Milner）** 的 **let 多态**类型推导；
- 带 **occurs-check** 的 **合一（unification）**，拒绝递归（无限）类型；
- 针对**可变引用**的**值限制（value restriction）**，防止不健全的泛化；
- 类型冲突错误定位到**冲突表达式的位置**（含相关位置附注）；
- 一个 CBV **求值器**，用来*实证*：关掉值限制后，被错误接受的程序在运行时
  崩溃（经典“共享引用”反例）；
- 仅用标准库的 **JSON over HTTP** 服务与命令行工具；
- 完整的**类型推导过程轨迹（derivation trace）**。

只使用 Python 3.8+ 标准库，无第三方依赖。

```
b/
├── miniml/                 # 工具链库
│   ├── span.py             # 源码位置 Pos / Span
│   ├── lexer.py            # 手写词法分析（嵌套注释、关键字、运算符）
│   ├── ast_nodes.py        # AST（每个节点带 Span）
│   ├── parser.py           # 手写递归下降 + Pratt 优先级
│   ├── types.py            # 类型表示(union-find)、合一、occurs、Scheme
│   ├── infer.py            # Algorithm W、值限制、错误定位、推导轨迹
│   ├── eval.py             # CBV 求值器（用于运行时反例验证）
│   ├── errors.py           # 带源码片段的编译器风格错误渲染
│   ├── pipeline.py         # 解析→推导→(求值) 编排 + JSON 序列化
│   ├── service.py          # stdlib HTTP JSON 服务 (/infer /eval /health)
│   └── cli.py              # 命令行 (infer | eval | lex | parse)
├── examples/               # 示例程序、请求/响应样例、反例演示
│   ├── *.mlml
│   ├── requests/*.json
│   ├── responses/*.json
│   └── demo_unsound.py
└── tests/                  # unittest 自动化测试（73 个用例）
```

---

## 1. 语言语法（本文档即权威定义）

### 1.1 词法

- **整数**：非空数字序列 `0-9`（如 `42`）。数字后紧跟字母是词法错误。
- **布尔**：关键字 `true`、`false`。
- **单位值**：关键字 `unit`（类型 `unit`；赋值等以 unit 为结果）。
- **标识符**：字母/下划线开头，后跟字母/数字/下划线。
- **关键字**：
  `let rec in fun if then else true false ref unit`
- **运算符/标点**：
  `+ - * / = <> < <= > >= && || := -> ! ( ) ; ,`
- 多字符运算符最长匹配：`<=`、`>=`、`<>`、`:=`、`->`、`&&`、`||`。
- **注释**：`(* ... *)`，可**嵌套**，未闭合报错并给位置。
- 顶层定义以 **`;;`** 结束；程序也可以是单个尾随表达式。

### 1.2 文法（EBNF）

```ebnf
program  := item* final-expr?
item     := toplet (';;' | EOF)
toplet   := 'let' 'rec'? IDENT '=' expr
final-expr := expr (';;')?

expr     := let-expr | fun-expr | if-expr | seq-expr
let-expr := 'let' 'rec'? IDENT '=' expr 'in' expr
fun-expr := 'fun' IDENT '->' expr
if-expr  := 'if' expr 'then' expr ('else' expr)?
seq-expr := assn-expr (';' assn-expr)*

assn-expr := or-expr (':=' assn-expr)?        (* 右结合 *)
or-expr  := and-expr ('||' and-expr)*
and-expr := cmp-expr ('&&' cmp-expr)*
cmp-expr := add-expr CMP add-expr ?           (* 比较不可串联 *)
add-expr := mul-expr (('+'|'-') mul-expr)*
mul-expr := app-expr (('*'|'/') app-expr)*
app-expr := prefix-expr aterm*                (* 左结合， juxtaposition *)
prefix-expr := '-' prefix-expr
             | 'ref' prefix-expr
             | '!'   prefix-expr
             | aterm
aterm    := INT | 'true' | 'false' | 'unit' | IDENT | '(' expr ')'

CMP      := '=' | '<>' | '<' | '<=' | '>' | '>='
```

`let rec` 的右侧**必须**是一个函数 `fun ... -> ...`（解析期强制）。

### 1.3 优先级与结合性（从松到紧）

| 优先级 | 运算符 | 结合性 |
|---|---|---|
| `;` 序列 | 左 |
| `:=` | **右** |
| `||` | 左（短路） |
| `&&` | 左（短路） |
| `= <> < <= > >=` | 不可串联 |
| `+ -` | 左 |
| `* /` | 左 |
| 函数应用（并置） | 左 |
| 前缀 `-`、`ref`、`!` | — |
| 原子（字面量、变量、括号） | — |

### 1.4 类型与类型规则（概览）

类型：`int`、`bool`、`unit`、`T -> T`（函数）、`ref T`（可变引用）。
类型变量印为 `a, b, c, ...`；多态方案印为 `forall a. ...`。

| 构造 | 规则 |
|---|---|
| `n` | `: int`；`true/false : bool`；`unit : unit` |
| `fun x -> e` | 若 `x : a`、`e : b`，则 `fun x -> e : a -> b` |
| `f e` | `f : a -> b`，`e : a` ⇒ `b` |
| `let x = e1 in e2` | 见 §3：可泛化时 `x` 绑定多态方案 |
| `if c then t else e` | `c : bool`，`t` 与 `e` 同型，结果为该型 |
| `if c then t`（无 else） | `c : bool`，`t : unit`，结果 `unit` |
| `e1 ; e2` | 计算并丢弃 `e1`，结果类型为 `e2` 的类型 |
| `ref e` | `e : a` ⇒ `ref a` |
| `!e` | `e : ref a` ⇒ `a` |
| `e1 := e2` | `e1 : ref a`，`e2 : a` ⇒ `unit` |
| `+ - * /`、一元 `-` | `int -> int -> int`（`/` 向零取整，除零运行时报错） |
| `&& ||` | `bool -> bool -> bool` |
| `= <>` | `a -> a -> bool`（受限的相等多态） |
| `< <= > >=` | `int -> int -> bool` |

---

## 2. 类型系统实现

### 2.1 类型表示与 union-find（`types.py`）

- `TVar` 是 union-find 变量，`link` 指向另一变量或结构类型；
  `prune()` 沿链压缩路径。
- `TCon`（`int/bool/unit/->/ref`）与 `TApp(con, args)` 表示结构类型；
  `a -> b` 即 `TApp(->, [a,b])`，`ref a` 即 `TApp(ref, [a])`。
- 自由变量集合用“规范代表的 **id 集合**”维护（`collect_vars`）：
  变量被链接到另一个变量时，贡献的是代表 id，而不是旧 id——否则在
  `let rec` 自引用场景下会把已约束变量错误量化进方案（开发中实际踩到并修复）。

### 2.2 合一与 occurs-check（`types.unify`）

```
unify(α, β)：先 prune；
  两边都是变量且同一：返回；
  一边是变量 α、另一边是 t：
      先做 occurs-check：若 α 出现在 t 中 → 报“无限类型”（cycle）
      否则 α.link := t；
  两边都是结构类型：构造子与参数个数必须相同，递归合一各参数；
  构造子冲突（如 int 对 bool）→ UnifyError。
```

典型拒绝：`fun x -> x x` 令 `x` 的类型 `a` 与 `a -> b` 合一，
`a` 出现在 `a -> b` 中，occurs-check 失败。

### 2.3 Algorithm W 与 let 多态（`infer.py`）

- 每个 `let`/顶层绑定：推出值的类型 `t`，把其中**不出现于当前类型环境**的
  自由变量**泛化**为 `forall`；每次使用该名字都用全新变量**实例化**方案。
  因此恒等函数 `id : forall a. a -> a` 可在 `id 1`、`id true`、`id id` 中
  各自独立实例化，互不污染。
- `let rec f = fun ...`：先在环境里为 `f` 放一个单态假设变量，检查函数体后
  与其合一；**泛化时把 `f` 自身从环境中暂时移除**（否则它所有变量都被当成
  环境自由变量而无法量化——开发中修复的第二个多态 bug）。

### 2.4 值限制（value restriction）

只有当绑定右侧是**语法值**（`fun`、字面量、变量、或包着这些值的括号）时，
才进行多态泛化。像 `let f = !r`（解引用是一次计算）这样的非值绑定，其
自由类型变量保持单态，直到被后续使用约束。

这对**可变引用**至关重要。反例：

```
let r = ref (fun x -> x) in   (* 一个持有 id 的共享可变单元 *)
r := (fun n -> n + 1);        (* 用 int -> int 的函数覆盖它 *)
let f = !r in                 (* 若错误地把 f 泛化为 forall a. a -> a：*)
let a = f true in             (*   当作 bool -> bool 使用 *)
let b = f 0 in                (*   又当作 int -> int 使用 *)
b
```

值限制开启时，`r` 的内容在 `:=` 后被固定为 `int -> int`，于是 `f true`
被拒绝（错误定位在 `f true` 的参数 `true`）。
值限制关闭时（`value_restriction=false`，本项目特意保留的开关），推导
错误地接受该程序并给 `f` 多态类型；随后的求值在 `true + 1` 处崩溃——
这直接演示了为什么需要该限制。

### 2.5 错误定位

每次合一都通过 `unify_at(actual, expected, span, what, related)` 报告：
错误携带触发冲突的表达式 `Span`（行:列），并可附相关位置（如
“函数在此期望……”）。错误码：

- `E001` 词法错误；`E002` 语法错误；
- `E003` 类型不匹配；`E004` occurs-check（无限类型）；
- `E005` 求值期 panic（仅在不健全接受时出现，用作反例证据）。

### 2.6 推导轨迹

推理时可记录结构化事件（`enter-lambda / instantiate / unify /
inferred / generalize / enter-rec`），CLI 用 `infer`（默认开启，
`--no-trace` 关闭）打印，服务放在响应的 `trace` 字段中。

---

## 3. 快速开始

```bash
# 类型推导（打印每个顶层绑定的方案 + 推导轨迹）
python3 -m miniml.cli infer examples/identity.mlml

# 推导并求值
python3 -m miniml.cli eval  examples/recursion.mlml

# 期待被 occurs-check 拒绝
python3 -m miniml.cli infer examples/occurs_check.mlml

# 引用反例：两种模式对照 + 真实运行崩溃演示（退出码 0，内置断言 --check）
python3 examples/demo_unsound.py --check
```

### 恒等函数的多次实例化（验收点 1）

```
$ python3 -m miniml.cli infer examples/identity.mlml --no-trace
val id : forall a. a -> a
val a : int
val b : bool
val c : a -> a  (monomorphic: value restriction)
val d : int
- : a -> a
```

注意 `c = id id`：`id id` 是一次**应用（非语法值）**，因此结果按值限制保持
单态（弱类型 `a -> a`），这与 SML 的弱类型行为一致；而 `id` 本身是值，
始终多态。

### 递归类型被拒绝（验收点 2）

```
$ python3 -m miniml.cli infer examples/occurs_check.mlml
error[E004]: argument type: occurs check failed; infinite type: ...
 --> examples/occurs_check.mlml:2:23
  |
2 | let loop = fun x -> x x ;;
  |                     - ^^^ argument type: occurs check failed; ...
```

### 引用不健全反例（验收点 3）

见 §2.4 与 `examples/demo_unsound.py`：
- 值限制 **ON**：推导阶段拒绝（E003，位置 `5:11`）；
- 值限制 **OFF**：推导接受，给 `f : forall a. a -> a`，求值在
  `operator '+': expected int but got true` 处 panic（E005）。

---

## 4. JSON 服务

```bash
python3 -m miniml.service --host 127.0.0.1 --port 8000
```

- `POST /infer` —— 解析 + 类型推导。
- `POST /eval` —— 先推导，成功后求值。
- `GET /health` —— 存活检查。

请求体字段：

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `source` | string | 必填 | MiniML 源码 |
| `value_restriction` | bool | `true` | 是否启用值限制 |
| `trace` | bool | `true` | 是否返回推导轨迹 |

`curl` 示例（样例文件在 `examples/requests/`，对应响应保存在
`examples/responses/`）：

```bash
# 多态恒等函数（200）
curl -s -X POST localhost:8000/infer \
  -H 'Content-Type: application/json' \
  --data @examples/requests/infer_identity.json

# occurs-check 拒绝（400，错误带行列号）
curl -s -X POST localhost:8000/infer \
  -H 'Content-Type: application/json' \
  --data @examples/requests/infer_occurs_check.json

# 引用反例：值限制开启 → 400 拒绝
curl -s -X POST localhost:8000/infer \
  -H 'Content-Type: application/json' \
  --data @examples/requests/infer_reference_rejected.json

# 同一程序：值限制关闭 → /infer 200 接受；/eval 400 运行时崩溃
curl -s -X POST localhost:8000/eval  \
  -H 'Content-Type: application/json' \
  --data @examples/requests/infer_reference_unsound.json
```

成功响应（节选）：

```json
{
  "ok": true,
  "bindings": [
    {"name": "id", "type": "forall a. a -> a",
     "monomorphic": false, "quantified": ["t0"]}
  ],
  "final_type": "a -> a",
  "trace": [
    {"step": "generalize", "detail": "id : a -> a  =>  forall a. a -> a", "location": "1:5"},
    {"step": "instantiate", "detail": "variable 'id': forall a. a -> a  =>  a -> a  (fresh: t1)", "location": null},
    {"step": "unify", "detail": "argument type: int ~ a", "location": "1:..."}
  ]
}
```

错误响应（400）：

```json
{
  "ok": false,
  "error": {
    "code": "E003",
    "phase": "infer",
    "message": "argument type: expected int, but got bool",
    "location": {"start": {"line": 4, "column": 11}, "end": {"line": 4, "column": 15}},
    "rendered": "error[E003]: ...（带源码片段和光标的终端风格文本）"
  }
}
```

---

## 5. 库 API

```python
from miniml import parse, infer_program, type_str, scheme_str
from miniml.pipeline import compile_source, evaluate

program = parse("let id = fun x -> x in id 1")
result  = infer_program(program)                 # value_restriction=True
print(type_str(result.expr_type))                # "int"
for b in result.bindings:
    print(b.name, scheme_str(b.scheme), b.monomorphic)

# 一步到位（错误时抛 CompileFailure，带 rendered 文本和 span）
program, result = compile_source(src, value_restriction=True, trace=True)
ev = evaluate(program)                            # 仅在推导成功后调用
print(ev.output)
```

---

## 6. 测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖（73 个用例）：

- **词法**：关键字/运算符、最长匹配、位置行列、嵌套注释、非法字符/数字；
- **语法**：顶层结构、inline let、`let rec` 仅函数、全部优先级/结合性、
  括号 span、解析错误位置；
- **合一**：变量绑定、构造子冲突、箭头分解、直接/间接 occurs-check；
- **let 多态**：恒等函数多实例化（int/bool/函数）、实例互不污染、
  多态递归函数、factorial；
- **occurs-check**：`x x`、`fun f -> f f`，并核对报错位置；
- **值限制/引用**：反例在 VR 开时拒绝（定位第 5 行）、关时接受、
  非值绑定单态、值绑定泛化、普通引用操作；
- **错误定位**：未绑定变量、int/bool 冲突行列、函数/实参相关位置、
  分支不一致、多行错误；
- **求值器**：算术、整除向零、除零 panic、布尔、递归阶乘、引用、
  以及“关 VR 后反例程序运行时 panic”；
- **HTTP 服务**：健康检查、成功/错误 JSON、错误码、位置、VR 开关、
  缺字段/非法 JSON、trace 开关（使用临时端口真实起服务）。

---

## 7. 设计取舍与边界

- **无 ADT / 模式匹配**：语言刻意精简，聚焦多态、合一、occurs 与引用。
  相等运算 `= / <>` 是受限多态（两侧同型即可），不实现完整 equality type。
- **单参数函数**：多参数用柯里化 `fun a -> fun b -> ...`。
- **值限制采用“严格值限制”**（而非弱类型变量）：非语法值一律不泛化，
  规则简单、对引用健全；代价是部分本可安全的多态被保守单态化。
- **求值器是无类型的**，仅用于运行已被接受的程序，并作为“关掉 VR 后
  系统不健全”的动态证据；生产路径始终先推导。
- 全部解析/分析为**手写实现**；没有调用任何外部编译器或解析/推导框架。
