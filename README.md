# Interval AI — 区间抽象解释后端（纯 Python，零依赖）

一个小语言（**IntervalLang**）的纯后端工具链：自己实现词法分析、递归下降语法
分析、整数 IR/控制流图、**区间抽象解释**（循环用**扩大 widening + 收窄
narrowing**），并报告两类错误的**可能性**：

* **除零 / 模零**（`/`、`%`）
* **数组下标越界**（含**负下标**）

算术按**数学整数**（任意精度，无溢出回绕）；除法向零取整，`%` 是与之配套的
截断余数（余数符号跟随被除数）。核心原则：**区间未知（Top）绝不被当成安全**。

没有任何前端 / UI。提供命令行、标准库实现的 JSON HTTP 服务、请求样例和自动化
测试（含**穷举有界输入对照真实执行**的差分验证）。

---

## 1. 目录结构

```
interval_ai/
  errors.py      带源码位置(Span)的错误类型
  lexer.py       手写词法分析器（逐字符扫描，保留每个 token 的行列/偏移）
  ast_nodes.py   AST（每个节点带 span）
  parser.py      手写递归下降解析器（优先级爬升；比较运算不可连写）
  ir.py          整数 IR：AST 降阶为基本块 CFG（指令携带源码位置）
  intervals.py   区间抽象域（join/meet/widen/narrow，数学整数算术）
  analyzer.py    CFG 上的区间不动点分析（扩大+收窄）、条件精化、报警
  concrete.py    具体参考解释器（真实执行，数学整数语义）
  diffcheck.py   穷举有界输入，抽象结果 vs 真实执行的差分验证
  pipeline.py    source -> AST -> CFG -> 结果 的串联与 JSON 序列化
  cli.py         命令行
  service.py     JSON HTTP 服务（仅用标准库 http.server）
examples/
  programs/      .ivl 示例程序
  requests/      HTTP 请求样例（JSON）
scripts/
  validate.py    对所有示例跑分析+穷举对照，打印保守性边界报告
tests/           unittest 自动化测试（域 / 前端 / 分析 / 穷举差分）
```

---

## 2. 语言语法（IntervalLang）

整段程序 = 若干**声明**后跟一个程序体（可显式 `{ ... }`，也可直接写语句序列）。

```ebnf
program   := declaration* ( block | statement* )

declaration := "var" IDENT ("=" expr)? ";"          # 普通标量，缺省为未定值(⊥)
             | "input" "var" IDENT ";"             # 未知整数输入（区间为 Top）
             | "input" "array" IDENT "[" INT "]" ";"  # 输入数组，零初始化

statement := block
           | "var" IDENT ("=" expr)? ";"           # 局部标量声明
           | lvalue "=" expr ";"
           | "input" IDENT ";"                     # 语句处再读一个输入
           | "havoc" IDENT ";"                     # 非确定性：置为任意整数(Top)
           | "if" "(" expr ")" block ("else" block)?
           | "while" "(" expr ")" block
           | ("skip" | ";")

lvalue    := IDENT | IDENT "[" expr "]"

block     := "{" statement* "}"

expr      := IDENT | INT | "true" | "false"
           | IDENT "[" expr "]"                    # 数组读
           | ("-" | "!") expr
           | expr ("+"|"-"|"*"|"/"|"%") expr
           | expr ("<"|"<="|">"|">="|"=="|"!=") expr
           | expr ("&&"|"||") expr
           | "(" expr ")"
```

说明与约定：

* **整数**：十进制、任意大小（数学整数）。没有浮点、没有位宽。
* **运算符优先级**（从低到高）：`||`，`&&`，`== !=`，`< <= > >=`，`+ -`，
  `* / %`，一元 `- !`。
* **比较运算不可结合**：`a < b < c` 是语法错误（避免歧义；用括号或 `&&`）。
* 条件中整数按“非 0 即真”处理；支持布尔字面量和 `! && ||`。
* 标量必须先用 `var`/`input var`/局部 `var` 声明；数组必须 `input array`
  声明并给常量长度。下标用 `a[i]`。
* 标量在声明时**零分配**：即使 `var x = e;` 写在某分支里，未进入该分支的
  路径上 `x` 也有确定初值 0（抽象与具体执行一致），而带初始化器的赋值只在
  其文本位置（含守卫）执行。
* 注释：`//` 行注释，`/* ... */` 块注释（可嵌套）。
* 每个 token/AST 节点/IR 指令都保留源码位置（1 基行、列 + 0 基偏移），报警
  的 `/`、`%` 指向**运算符本身**，下标越界指向数组名处。

### 运算语义（抽象与具体一致）

* `/`：向零取整（C 风格 truncation），如 `-7/2 = -3`（不是 Python 的 `-4`）。
* `%`：截断余数 `a - trunc(a/b)*b`，符号跟随被除数，如 `-7 % 2 = -1`。
* 除数区间含 0 ⇒ 报除零/模零；数组下标区间不完全包含于 `[0, len-1]` ⇒ 报越界。

---

## 3. 抽象解释做了什么

### 3.1 抽象域

每个标量变量映射到一个整数区间 `[lo, hi]`，端点可以是有限整数或
`-∞/+∞`（JSON 中用 `null` 表示），另有 `⊥`（不可达）。

* `join`：凸包并；`meet`：交。
* **widen（扩大）**：在循环头，凡是相对上一轮“不稳定”的有限界直接推到无穷，
  保证升链有限步终止。
* **narrow（收窄）**：扩大得到后继不动点后做下降迭代，用经典 narrowing 只允许
  “原本是无穷”的界被循环/守卫信息重新收紧（有限界不会被放松，保证终止）。

例：`i=0; while(i<10){i=i+1;}`
扩大阶段循环头 `i: [0,0] → [0,+∞]`；收窄阶段由守卫 `i<10` 恢复出循环头
`i∈[0,10]`、循环体 `i∈[0,9]`、出口 `i==10`。

### 3.2 不动点计算

AST 先降阶为**基本块 CFG**（分支变为带条件的两条边）。分析器：

1. 用支配关系求回边，回边目标＝**循环头**（扩大点）；
2. 对每个循环头用支配/前驱计算其**自然循环体**，得到该循环真正修改的
   变量集合（这使嵌套循环中内层头**不会**扩大外层计数器）；
3. 逆后序工作列表做**带扩大的混沌上升迭代**（只对循环变量 widening，
   其余携带变量在所有边上单调 join）；
4. 再做**带收窄的下降迭代**（循环头只对其循环变量做经典 narrowing，其余
   精确重算），并只在收到合法后置不动点（`incoming ⊆ old`）时收窄，保证嵌套
   循环下仍终止且健全。

### 3.3 条件精化（守卫）

沿分支边用条件过滤区间：`k > 0` 的 then 边把 `k` 收为 `[1,+∞)`，从而
`9 / k` 无除零报警；`i>=0 && i<4` 可证明 `a[i]`（长度 4）安全。条件对非
区间常量的一侧精确，对 `&&/||` 做可判定的分解。

### 3.4 健全性（soundness）与有意的精度边界

* 未知即**不安全**：`Top` 除数、`Top` 下标都会报警；数组**内容**不逐格跟踪，
  数组读返回 `Top`（只做下标界检查）。
* 报警分 `possible`（可能，区间含坏值但也含好值）与 `certain`（确定，例如
  常量 `1/0`、`a[4]`、`a[-1]`，或区间整体落在非法侧）。
* 不可达块（被矛盾守卫排除）内的操作**不**报警。
* 这是**非关系域**，因此存在**有意的误报**（spurious），例如：
  * 相关变量 `y=x` 时，`x-y` 恒为 0，但独立区间无法推出（见
    `examples/programs/spurious_correlation.ivl`）；
  * 数组单元已被赋常量，数组读仍是 `Top`（`spurious_array.ivl`）；
  * 未知输入界定的循环，循环计数高界停在 `+∞`（`unknown_loop.ivl` 在安全
    输入区域仍报“可能越界”）。

这些都在穷举对照中明确列成 **spurious/conservative alarms**，而不是藏起来。

---

## 4. 命令行用法

无需安装，Python 3.10+ 标准库即可（开发环境为 3.12）。

```bash
# 区间分析，输出 JSON（每块不变量 + 报警，含源码位置）
python3 -m interval_ai.cli analyze examples/programs/safe_loop.ivl

# 从标准输入读源码
echo 'input var k; var z = 1/k;' | python3 -m interval_ai.cli analyze -

# 具体执行（真实跑，整数输入按声明/语句顺序给出）
python3 -m interval_ai.cli run examples/programs/unknown_loop.ivl 6
```

`analyze` 输出里：

* `invariants[b]`：第 `b` 个基本块**入口**处每个变量的区间；
  `reachable:false` 表示 `⊥`。
* `alarms[]`：`kind`（`div_by_zero` / `index_out_of_bounds`）、`certainty`
  （`possible`/`certain`）、`message`、`location`（行列偏移），越界还带
  `index_interval` 与 `array_length`。

---

## 5. JSON HTTP 服务

仅用标准库 `http.server`：

```bash
python3 -m interval_ai.service --host 127.0.0.1 --port 8080
```

| 方法/路径 | 请求体 | 说明 |
|---|---|---|
| `POST /analyze` | `{"source": "..."}` | 区间分析，返回不变量与报警 |
| `POST /run` | `{"source": "...", "inputs": [int,...]}` | 具体执行；出错时带 `runtime_error` |
| `GET /healthz` | — | `{"status":"ok"}` |

词法/语法/分析错误返回 HTTP 400 与 `{"status":"error", ...}`（带位置）。

用样例发请求：

```bash
curl -s -X POST http://127.0.0.1:8080/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/requests/analyze_loop.json

curl -s -X POST http://127.0.0.1:8080/run \
  -H 'Content-Type: application/json' \
  --data @examples/requests/run_ok.json
```

样例文件：`examples/requests/{analyze_safe,analyze_loop,run_ok,run_divzero}.json`。

### `/analyze` 响应结构（节选）

```json
{
  "status": "ok",
  "variables": ["k", "z"],
  "arrays": {},
  "num_blocks": 3,
  "entry_block": 0,
  "invariants": [
    {"block": 0, "reachable": true,
     "variables": {"k": {"lo": null, "hi": null},
                   "z": {"bottom": true}}}
  ],
  "alarms": [
    {"kind": "div_by_zero", "certainty": "possible",
     "message": "possible division by zero: divisor interval [-∞, +∞] contains 0",
     "block": 0,
     "location": {"start": 23, "end": 24, "line": 1, "col": 24}}
  ],
  "alarm_count": 1,
  "possible_alarm_count": 1,
  "certain_alarm_count": 0
}
```

区间 `{"lo": null, "hi": null}` 即 `Top`；`{"bottom": true}` 即 `⊥`。

---

## 6. 验证方法（验收）

### 6.1 自动化测试

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

* `test_intervals.py`：区间域（含无穷端点乘法符号、截断除/余、widen/narrow）。
* `test_frontend.py`：词法位置、注释、优先级、非结合比较、非法程序、IR 降阶
  与位置、局部声明必须留在守卫分支内。
* `test_analysis.py`：守卫消警、扩大/收窄后的循环不变量、除零/越界的
  possible/certain、不可达块不报警、负下标、数组读为 Top、大整数数学精度。
* `test_diffcheck.py`：**穷举有界输入对照真实执行**（见下）。

### 6.2 穷举差分验证（核心验收）

`interval_ai/diffcheck.py` 对程序的每个标量输入在一个有界整数区域做笛卡尔积
穷举，用**具体解释器真实执行**每个点，再与抽象结果逐条核对：

1. **不漏报**：任何一个具体点在某站点真实除零/越界 ⇒ 分析器必须报该站点
   （`MISSED` 必须为空）；
2. **不变量包含**：每个终止执行的最终变量值必须落在某退出块的抽象区间内；
3. **保守性边界**：分析器报了“可能”，但穷举区域内**没有任何点**真的崩 ——
   记为 `spurious`，用来展示域的精度损失（相关变量、数组内容、未知循环界）。

一键报告：

```bash
python3 scripts/validate.py
```

它覆盖：安全可证的有界循环、相关变量精度损失（真崩与纯误报两种）、负下标
确定越界、双侧守卫安全下标、未知界循环（同时给“会崩的区域 n∈[-3,8]”和
“安全区域 n∈[0,4]”两种输入区）、截断取模、以及数组内容 Top 的误报。

> 输入数组在差分验证中固定为零初始化（非确定性只来自标量输入），因此穷举是
> 对标量输入区域的完整覆盖；这一点在脚本与报告中显式说明。

---

## 7. 设计取舍与局限

* **非关系区间域**：不跟踪变量间关系（`x-y`、线性不等式），故有上述误报；
  可作为将来升级到差分约束/八边形/谓词域的起点。
* **数组内容用单一 Top 抽象**：只做下标界检查，不做强更新/弱更新。这是明确
  的简化，确保“未知不安全”，代价是数组值相关的精度损失。
* **不可达性用 ⊥ 精确处理**，但条件精化只针对“变量 与 常量/单变量相等”等
  可精确处理的形状；一般非线性/两变量关系保持区间不变（仍健全）。
* 具体解释器有步数上限，防止在抽象无法证明终止的程序上无限执行。
* 纯后端：不做任何解析器/编译器库的复用，词法、语法、IR、分析均为手写。
