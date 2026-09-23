# tinyinfer — 带 let 多态的函数式小语言类型推导工具链

一个**纯后端**的小语言工具链库 + JSON 服务，从零手写：

- 手写词法分析器（逐字符扫描，记录每个 token 的行列位置）；
- 手写递归下降 / Pratt 语法分析器（不使用任何解析器生成器或现成编译器）；
- 基于 **Hindley–Milner 算法 W** 的类型推导：合一（unification）、
  **occurs-check**、let 多态的一般化（generalize）与实例化（instantiate）；
- **值限制（value restriction）**，阻止可变引用被非法一般化；
- 一个最小 tree-walk 求值器，用来实际运行（并证伪）不健全程序；
- 标准库 `http.server` 实现的 JSON HTTP 服务与命令行工具。

只依赖 Python 3.10+ 标准库，无第三方依赖。

---

## 1. 语言规范（自定义语法）

### 1.1 词法

| 类别 | 形式 |
|---|---|
| 整数 | `0`、`42`（非负整数字面量） |
| 布尔 | `true`、`false` |
| 单位 | `unit` |
| 标识符 | 字母/下划线开头，后接字母数字下划线 |
| 关键字 | `let rec in fun if then else true false ref deref unit` |
| 运算符 | `+ - * / == != < <= > >= <- ; ( ) : ->` |
| 行注释 | `//` 到行尾 |
| 块注释 | `(* ... *)`，可嵌套 |

`not` 不是关键字，而是一个内建多态函数（标识符），因此既可以写
`not true`，也可以把 `not` 本身作为值传递（如 `twice not`）。

### 1.2 表达式（优先级从低到高）

```
program := toplet* expr?
toplet  := "let" "rec"? IDENT param* (":" type)? "=" expr
expr    := let-expr | if-expr | fun-expr | seq
seq     := assign (";" assign)*
assign  := cmp ("<-" assign)?          (* 右结合 *)
cmp     := additive (("=="|"!="|"<"|"<="|">"|">=") additive)*
additive:= mul (("+"|"-") mul)*
mul     := unary (("*"|"/") unary)*
unary   := ("ref"|"deref") unary | app
app     := atom atom*                  (* 左结合，柯里化 *)
atom    := INT | "true" | "false" | "unit" | IDENT
         | "(" expr ")"
         | "fun" param+ "->" expr
         | "let" "rec"? IDENT param* (":" type)? "=" expr "in" expr
         | "if" expr "then" expr "else" expr
param   := IDENT | "(" IDENT ":" type ")"
```

- 应用左结合：`f x y` = `(f x) y`，天然表达柯里化。
- 顶层定义之间以 `let` 自然分隔；定义后想接任意收尾表达式，
  用行内 `let ... in`。
- 顶层定义右值中的 `fun`/`if` 内部可以自由使用行内 `let ... in`。

### 1.3 类型与标注

```
type := "'" IDENT | IDENT | type "ref" | "(" type ")" | type "->" type
```

基础类型 `int`、`bool`、`unit`；`int ref` 是指向 int 的可变单元；
函数类型 `->` 右结合（`int -> bool -> int`）。

可在定义处写标注：

- 参数标注：`let f (x: int) = ...`、`fun (x: int) -> ...`
- 结果标注（要求所有参数都标注）：`let f (x: int): int = ...`
- 多态标注：`let f (x: 'a): 'a = x`

### 1.4 内建

| 名称 | 类型方案 | 说明 |
|---|---|---|
| `ref` | `∀'a. 'a -> 'a ref` | 用初值创建可变单元 |
| `new_ref` | `∀'a. unit -> 'a ref` | 创建空单元（赋值前 deref 报错），用于演示多态+引用的不健全性 |
| `not` | `bool -> bool` | 布尔取反 |

引用操作：`deref r` 读取，`r <- v` 赋值（产生 `unit`），`e1; e2` 顺序执行。

---

## 2. 类型推导：算法 W

核心在 `tinyinfer/inference.py`。

### 2.1 数据结构

- `TVar("?n")`：未定类型变量；`TCon` 基础类型；`TApp` 类型构造
  （`ref` 单元）；`TFun` 函数类型。
- **替换表** `subst: dict[str, Type]`，合一只做变量绑定，`apply()`
  沿替换链化约。
- `Scheme(vars, body)`：类型方案 `∀vars. body`。

### 2.2 合一与 occurs-check

`unify(t1, t2)` 把两个类型化约后逐情形统一；当要把变量 `a` 绑定到
类型 `t` 时，先调用 `occurs(a, t)`：

> 若 `a` 出现在 `t` 中，则绑定会构造出无限（递归）类型，立即抛
> `OccursError` 并指向冲突表达式。

### 2.3 let 多态

- `let x = e in b`：推出 `e : t` 后，`generalize(t)` 把其中**不自由
  于当前环境**的变量提升为 `∀` 约束变量，得到方案；
- 使用 `x` 时 `instantiate` 用全新变量替换约束变量——因此同一个
  `id` 可以在一处按 `int -> int`、另一处按 `bool -> bool` 使用，
  两次实例化相互独立。
- 无标注的 `let rec` 名称在右值推导期间是**单态**的（标准
  Damas–Milner 不支持多态递归；多态递归需显式标注）。

### 2.4 值限制（限制可变引用的泛化）

朴素算法 W 对**所有** let 右值都一般化。配合可变引用时这是不健全的
（见下节）。本实现默认采用严格**值限制**：

> 只有当 let 右值是**语法值**（函数、字面量、变量）时才一般化；
> 函数应用、`ref`/`new_ref` 等计算式表达式保持单态。

CLI/API 可用 `value_restriction=false` 切回 naive W 以复现反例。

---

## 3. 三个验收点

### 3.1 恒等函数的多次实例化 — `examples/identity.tml`

```
let id = fun x -> x in
let a = id 1 in       (* id : int -> int   *)
let b = id true in    (* id : bool -> bool *)
let c = id id 7 in    (* 两次独立实例化 *)
if b then a + c else 0
```

`id` 被一般化为 `∀'a. 'a -> 'a`；三处使用产生互不影响的新变量。
推导结果 `int`，求值 `8`。`--trace` 可看到每次 `generalize`/
`instantiate`/`unify` 的完整过程。

### 3.2 递归类型拒绝 — `examples/recursive_type_reject.tml`

```
let loop = fun f -> f f in loop loop
```

推出 `f : ?a` 后，`f f` 要求 `?a ~ ?a -> ?t`；绑定 `?a` 前
occurs-check 发现 `?a` 出现在右侧函数类型中，报错：

```
[OccursError] occurs-check 失败：类型变量 'a 出现在 'a -> 'b 中，
无法构造递归（无限）类型   (4:23)
```

### 3.3 引用导致的不健全反例 — `examples/ref_unsoundness.tml`

```
let r = new_ref unit in
  r <- true;
  deref r + 1
```

- **naive W（无值限制）**：`r` 被一般化为 `∀'a. 'a ref`，第一次按
  `bool ref`、第二次按 `int ref` 使用，**类型检查通过**，最终类型
  还显示为 `int`；但运行到 `true + 1` 时求值器崩溃
  （`RuntimeFailure：算术运算 + 运行期收到非 int：bool(True) + 1`）
  ——类型系统的可靠性（soundness）被破坏。
- **值限制（默认）**：`new_ref unit` 是应用（计算式），`r` 保持单态，
  第二次不兼容使用在 `deref r + 1` 处产生 `bool/int` 冲突，
  **检查阶段即拒绝**，程序不会运行。

这正是 Wright (1994) 提出值限制要解决的经典问题在本语言中的对应形态。

---

## 4. 使用方式

无需安装第三方依赖，直接在仓库根目录运行。

### 4.1 命令行

```bash
# 类型推导 + 求值，打印类型、顶层方案与结果
python3 -m tinyinfer.cli infer examples/identity.tml

# 打印逐步推导过程（合一/一般化/实例化）
python3 -m tinyinfer.cli infer examples/identity.tml --trace

# 关闭值限制，复现引用不健全反例（检查通过、运行崩溃）
python3 -m tinyinfer.cli infer examples/ref_unsoundness.tml --naive

# 只做词法/语法分析
python3 -m tinyinfer.cli parse examples/identity.tml

# 输出服务同款 JSON
python3 -m tinyinfer.cli infer examples/identity.tml --json

# 从标准输入
echo "fun x -> x" | python3 -m tinyinfer.cli infer -
```

### 4.2 JSON HTTP 服务

```bash
python3 -m tinyinfer.server --port 8000
```

- `GET /health` → `{"ok": true, ...}`
- `POST /analyze`

请求：

```json
{
  "source": "let id = fun x -> x in id 1",
  "value_restriction": true,
  "annotate": true,
  "evaluate": true
}
```

后三个字段可选，默认均为 `true`。成功响应含 `type`、`bindings`
（名字→方案）、`value`（求值结果）、`eval_error` 与完整 `trace`。
词法/语法/类型错误返回 HTTP 400：

```json
{
  "ok": false,
  "error": {
    "kind": "UnifyError",
    "message": "类型冲突：期望 int，实际得到 bool（应用 f：函数参数类型）",
    "span": {"start": {"line": 2, "column": 3, ...}, ...},
    "snippet": "    2 | f true\n      |   ^^^^"
  }
}
```

请求样例见 `examples/request_*.json`，可用：

```bash
curl -s -X POST localhost:8000/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/request_identity.json
```

### 4.3 作为库

```python
from tinyinfer.pipeline import analyze, parse_source

result = analyze("let id = fun x -> x in id 1")
result["type"]               # "int"
result["bindings"]           # [{"name": "id", "scheme": "forall 'a. 'a -> 'a"}]
result["value"]              # "1"
for step in result["trace"]: # 逐步推导过程
    print(step["kind"], step["detail"])
```

---

## 5. 源码位置与错误定位

每个 token 都带 `Span(start, end)`（offset + 1 起行列），AST 节点
原样保留。类型错误在**引发合一失败的那个子表达式**处报告：

- 实参类型不匹配 → 指向实参；
- 条件不是 bool → 指向条件；
- if 两分支不一致 → 指向 else 分支；
- occurs-check → 指向构造出递归类型的应用表达式；
- 运算符操作数错误 → 指向具体操作数。

JSON 错误中的 `snippet` 直接给出源码行与 `^^^^` 指示。

---

## 6. 目录结构

```
tinyinfer/
  locations.py   源码位置 Position/Span 与片段渲染
  errors.py      带位置的错误体系（词法/语法/类型/求值）
  lexer.py       手写词法分析器
  ast.py         AST 与类型标注 AST
  parser.py      手写递归下降 + Pratt 解析器
  types.py       类型表示、替换、occurs 自由变量、方案、渲染
  inference.py   算法 W：合一/occurs-check/一般化/实例化/值限制/轨迹
  eval.py        最小 tree-walk 求值器（含可变单元）
  pipeline.py    解析→推导→求值 统一管道与 JSON 序列化
  server.py      标准库 JSON HTTP 服务
  cli.py         命令行
examples/        .tml 程序与 .json 请求样例
tests/           unittest 自动化测试（解析/推导/求值/HTTP 服务）
```

## 7. 运行测试

```bash
python3 -m unittest discover -s tests -v
```

设计边界：本语言是教学级核心演算，刻意不提供代数数据类型、模式匹配
与多态递归（无标注）；这些不影响对 let 多态、合一与值限制的验证。
