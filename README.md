# L0 常量传播格分析工具链（纯后端）

一个从零实现的小语言 **L0** 工具链，核心是 **稀疏条件常量传播
（Sparse Conditional Constant Propagation, SCCP）** 格分析，以及在其
结果上做的、可证明保持可观察语义的保守优化。全部用 Python 标准库实现，
**词法分析、语法分析、SSA 构造、格分析、优化与执行均为自研，不调用
任何现成编译器/解析框架。** 仅提供命令行与 JSON HTTP 服务，不含前端。

- 四值格：`top`（未定义/未知）/ `const(n)`（常量）/ `undef`（显式未定义
  poison）/ `bottom`（非常量）。区分乐观的 `top` 与确定的 `undef`，
  避免把可执行路径上的未定义值错误吸收进常量
- Wegman–Zadeck 风格 SCCP：**CFG 可达边工作流与 SSA 定值工作流联合
  求解**，不可达分支里的指令不会污染格
- 最小 SSA 构造：支配者 / 支配边界 / φ 插入 / Cytron 重命名
- 安全折叠：**不错误折叠除零/模零**，**不删除 `print` 等副作用**，
  不可达块才整体删除
- 双参考语义 + 差分模糊测试：AST 解释器（金标准）与 SSA IR 解释器
  （优化前后共用同一执行器），自动化验证输出与错误行为一致

---

## 1. 目录结构

```
constprop/             工具链库（纯标准库）
  source.py            源码位置(Span)与错误类型（lex/parse/runtime）
  lexer.py             手写词法分析
  ast.py               AST 定义
  parser.py            手写递归下降解析
  model.py             SSA IR、基本块、CFG、JSON/文本序列化
  runtime.py           运算语义（向零截断除法、比较、除零错误）
  cfg.py               AST -> 可化简 CFG（短路逻辑显式降低为分支）
  ssa.py               支配者/支配边界/φ 插入/重命名 -> 最小 SSA
  sccp.py              稀疏条件常量传播（四值格 + 双工作流）
  optimizer.py         常量具体化(RAUW) + 不可达删除 + φ收缩 + DCE
  interp.py            AST 参考解释器（金标准）
  ir_interp.py         SSA IR 解释器（优化前后共用）
  serialize.py         AST -> JSON
  service.py           JSON HTTP 服务（http.server，无第三方依赖）
  cli.py               命令行入口
examples/              示例程序(.l0)与 JSON 请求样例
tests/                 自动化测试（unittest）
run_tests.sh           运行全部测试
run_examples.sh        运行六个验收示例并比较前后行为
```

## 2. L0 语言（自定义语法，文档化定义）

整数命令式语言；只有整数，比较与逻辑运算产生整数 `0`/`1`。
整数除法 `/`、取模 `%` 采用**向零截断**（C/Java 语义，不依赖宿主语言）。

```ebnf
program     := statement*
statement   := block | if | while | print | assign
block       := '{' statement* '}'
if          := 'if' expr statement ('else' statement)?
while       := 'while' expr statement
print       := 'print' expr ';'
assign      := IDENT '=' expr ';'
expr        := orExpr
orExpr      := andExpr ('||' andExpr)*          # 短路
andExpr     := cmpExpr ('&&' cmpExpr)*          # 短路
cmpExpr     := addExpr (CMP addExpr)?           # 比较不允许连续
addExpr     := mulExpr (('+'|'-') mulExpr)*
mulExpr     := unary  (('*'|'/'|'%') unary)*
unary       := '!' unary | '-' unary | primary
primary     := INTEGER | 'true' | 'false' | IDENT | '(' expr ')'
CMP         := '==' | '!=' | '<' | '>' | '<=' | '>='
```

- 关键字：`if else while print true false`；行注释 `// …`。
- **作用域：函数级（flat）。** `{ }` 只是语句分组，不引入变量作用域；
  任何块内赋值都绑定到整个程序共享的变量。
- **未定义变量语义（惰性 undef）：** 读取从未赋值的名字得到一个
  `undef` 值；它在纯算术 / 比较 / 赋值中**惰性传播**，只有抵达观察点
  （`print` 参数、`if`/`while` 条件）才抛运行时错误
  `undefined-variable`。这样“读了未定义变量但结果从未被观察”的死
  表达式没有可观察效果，优化器可合法删除（与主流编译器对
  poison/undef 的处理一致）。
- **运行时错误（结构化、带稳定 code）：**
  - `division-by-zero`：`/` 或 `%` 的除数在运行期为 0；
  - `undefined-variable`：在观察点使用了未定义值；
  - `step-limit`：超过执行步进上限（防止死循环耗尽资源）。

### 示例

```
x = 40;
x = x + 2;                 // SCCP: x = const(42)
if (1) {
  print x;                 // 具体化 -> print 42
} else {
  print 999;               // 不可达
  z = 7 / 0;               // 不可达块内的除零：运行期不触发
}
print 10;
```

## 3. 安装与运行

只需 Python 3.10+，**无第三方依赖**。

```bash
# 全部自动化测试（92 个用例，含 1000 个随机程序差分测试）
./run_tests.sh

# 六个验收示例
./run_examples.sh
```

### 命令行

```bash
python -m constprop.cli parse    examples/01_unreachable_branch.l0
python -m constprop.cli ir       examples/02_loop_constant.l0
python -m constprop.cli analyze  examples/03_confluence.l0
python -m constprop.cli run      examples/04_divzero.l0
python -m constprop.cli optimize examples/01_unreachable_branch.l0
python -m constprop.cli serve --host 127.0.0.1 --port 8000
```

`optimize` 会打印 SCCP 常量、优化改写、优化后 IR，以及 **AST 金标准 /
优化前 IR / 优化后 IR** 三者的运行对比与等价性结论。

### JSON HTTP 服务

启动：`python -m constprop.cli serve --port 8000`。端点均为
`POST application/json`：

| 端点        | 作用                                           |
|-------------|------------------------------------------------|
| `/parse`    | 词法 + 语法，返回带 span 的 AST JSON           |
| `/ir`       | 返回优化前 SSA IR（JSON + 文本）               |
| `/analyze`  | 只跑 SCCP，返回可达块、可执行边、格结果        |
| `/optimize` | 分析 + 优化，返回前后 IR、改写报告、运行对比   |
| `/run`      | 用 AST 金标准语义执行                          |
| `GET /health` | 存活探针                                     |

词法/语法/运行时错误返回 HTTP 200 且 `"ok": false` 加结构化 `error`
（它们是正常 API 结果）；请求体不是合法 JSON 才返回 400。

```bash
curl -s -X POST http://127.0.0.1:8000/optimize \
  -H 'Content-Type: application/json' \
  --data @examples/request_optimize.json
```

请求样例见 `examples/request_*.json`。

## 4. IR 与 SSA 构造

每条指令保留源码 `Span`。指令：

```
lit    t = #n
copy   t = a
binop  t = op(a,b)     op ∈ + - * / % == != < > <= >=
unop   t = op(a)       op ∈ - !
phi    t = phi([pred:a], [pred:b], …)
print  print a                       （副作用）
br     br cond, thenLabel, elseLabel
jmp    jmp label
exit
```

SSA 构造（`ssa.py`）：逆后序 → Cooper–Harvey–Kennedy 迭代求直接支配者
→ 支配树 → 支配边界 → 在迭代边界上插入 φ（仅对源变量与跨分支合流的
短路结果临时值插入；普通表达式临时值单次定值不插 φ）→ Cytron 支配树
DFS 重命名。从未赋值的使用解析为 `__undef__` 哨兵。

## 5. SCCP 格分析（核心）

每个 SSA 值的格：

```
        top   尚未被任何*已执行*定义求值（含 φ 尚未执行的入边）——乐观
       / | \
 const(a) const(b) …   不同常量合流 → bottom
      | \|/ |
    undef  …           某条可执行路径上显式产生的未定义 poison
       \ | /
      bottom  非常量 / 无法静态确定
```

`meet`：

- `top ∧ x = x`（top 是单位元，乐观吸收“边尚未执行”的未知）；
- `const(a) ∧ const(b) = const(a)` 当 `a=b`，否则 `bottom`；
- `undef ∧ const = undef`、`undef ∧ undef = undef`
  （**undef 不被常量吸收**——它是已执行路径上的确定 poison）；
- `bottom` 吸收一切。

为什么要把 `undef` 从 `top` 里分出来：若把“可执行路径上确实算出的
未定义值”也当乐观 `top`，那么 `x = 0 || missing` 的结果会在合流时
被错误吸收为常量，优化后就丢失了本该在观察点抛出的
`undefined-variable`。`top` 只表示“这条边/定义还没执行到”，可以被
另一条已知常量边乐观覆盖；`undef` 表示“真的算出了未定义值”，必须
保留到运行期。

两个 FIFO 工作流联合推进：

1. **CFG 边工作流**：边只在其条件被格信息证实时才标记“可执行”。
   常量条件只加入被选中的边，另一分支保持不可达；`bottom` 条件两条边
   都加入；`top` 条件暂时一条边都不加（乐观等待）。
2. **SSA 工作流**：值的格下降时只重新求值其直接使用者（稀疏）。
   φ 是“按边”的使用：仅当对应入边可执行时该入边值才参与合流；新边
   到达或入边值下降都会重新合流 φ。

### 安全折叠红线

- **除零 / 模零不折叠**：当除数格值为常量 `0` 时，结果给 `bottom`
  而不是常量，指令原样保留 —— 它在运行期必然抛 `division-by-zero`，
  折叠会消灭这个错误。结果无人使用的 `z = 5/0` 同样**不**被 DCE 删除。
- **副作用不删除**：`print` 无目标、不产生格值，但优化只允许改写其
  常量参数，绝不删除指令；唯一的删除发生在承载它的块整体不可达时。
- **undef 不具体化**：引用未初始化变量的使用不会被替换成字面量。

优化器（`optimizer.py`）做四类保守改写：常量具体化（RAUW 所有使用点）、
常量分支 `br → jmp`、从入口重算可达集后删除不可达块并收缩 φ、死纯
指令 DCE（`/`、`%`、`print`、终结符均不参与 DCE）。

## 6. 语义保持的验证方式

- AST 解释器是语义金标准；SSA IR 解释器执行**优化前和优化后**同一份 IR。
- 等价性签名只比较**可观察行为**：逐行输出、错误分类 `error_code`、
  出错**行号**（不比较列号——IR 会把一个表达式拆成多条指令，读取点
  列号可能不同）。
- `tests/test_optimizer.py` 随机生成 1000 个结构良好、必终止的 L0
  程序（含分支、循环、合流、可能的除零与未定义使用），逐一比较
  AST / 优化前IR / 优化后IR 三者签名，任何不一致即失败。

## 7. 验收反例

| 程序 | 验收点 |
|------|--------|
| `01_unreachable_branch.l0` | 恒真分支：else 不可达并整体删除（含其中的 `7/0`）；输出 `42,10` |
| `02_loop_constant.l0` | 多趟循环 φ 回边合流后降为 `bottom`（不被错误折叠）；零趟循环保持常量并裁剪常量分支 |
| `03_confluence.l0` | 合流反例：两路不同常量 `20∧40→bottom`、相同常量 `7∧7→const(7)`（分支相关性）、`bottom` 传播 |
| `04_divzero.l0` | 常量除数 0 **不折叠**；优化前后都在同一行抛 `division-by-zero`，错误前输出一致 |
| `05_side_effects.l0` | 可达 `print` 保留、参数具体化；结果未使用的 `5/0` 不被 DCE，错误保留 |
| `06_undefined.l0` | 未定义值惰性传播，观察点抛 `undefined-variable`，错误前输出一致 |

---

## 实现状态 / 如实记录

- 全部功能已实现并实际运行通过；测试、示例、HTTP 服务均验证过。
- 差分模糊：默认测试套件内置 1000 个随机程序；开发中另用 4 个随机
  种子额外跑了 9000 个（含一元负号、嵌套短路、嵌套 if、循环、合流、
  除零与未定义使用），AST / 优化前 IR / 优化后 IR 三者签名零失配。
- 已知取舍：比较表达式不允许连续（`a < b < c` 直接报语法错误）；
  整数为 Python 任意精度（无溢出）；执行有 `step_limit` 上界
  （默认 2,000,000，可在请求/CLI 覆盖）。
