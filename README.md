# 字节码验证器（byteverifier）

一个从零实现的**小语言工具链 + 栈式字节码验证器 + JSON 服务**纯后端项目。
词法/语法/编译/验证/解释全部手写，**核心解析与分析不依赖任何现成编译器**
（仅用 Python 标准库；HTTP 服务基于 `http.server`）。

通过静态验证的字节码，在解释器中**不可能发生操作数栈下溢、未初始化局部量
读取、类型误用**。验证器还检查跳转边界（含回边）、合流点栈高度与类型、
局部量初始化，并输出从函数入口到出错点的**最短可读错误路径**。

---

## 1. 目录结构

```
byteverifier/
  common.py        源码位置 Span、统一错误 ToolError（阶段+错误码+最短路径）
  lexer.py         手写词法分析（保留行列位置、注释）
  ast_nodes.py     AST 定义（全部节点带 Span）
  parser.py        手写递归下降解析（Pratt 优先级）
  bytecode.py      栈式字节码：操作码/类型标签/二进制编解码/反汇编
  compiler.py      AST -> 字节码（标签回填、短路求值、max_stack 不动点）
  verifier.py      ★ 字节码验证器（抽象状态数据流不动点）
  interpreter.py   验证后执行的解释器（步数/递归深度资源限制）
  errorpath.py     CFG 上 Dijkstra 求最短错误路径并渲染
  mutate.py        单字节/单 bit 变异工具
  pipeline.py      编译->验证->运行 便捷流水线
  service.py       零依赖 JSON HTTP 服务
  cli.py / __main__.py   命令行
examples/          有效/无效示例程序
tests/             60 个 unittest 自动化测试
scripts/
  mutation_campaign.py  变异活动（穷举单 bit 翻转 + 定向变异）
```

---

## 2. 语言语法（VLang）

文件扩展名约定 `.vl`。语法（EBNF 风格）：

```ebnf
program   := func+
func      := type IDENT "(" [param ("," param)*] ")" block
param     := type IDENT
type      := "int" | "bool" | "void"          (* void 只能作返回类型 *)
block     := "{" stmt* "}"

stmt      := type IDENT ["=" expr] ";"        (* 变量声明，可无初值 *)
           | IDENT "=" expr ";"               (* 赋值（变量必须已声明） *)
           | IDENT "(" args ")" ";"            (* 仅表达式语句：函数调用 *)
           | "if" "(" expr ")" block
             ["else" (block | "if" ...)]      (* else-if 链 *)
           | "while" "(" expr ")" block
           | "return" [expr] ";"

expr      := 逻辑或 (|| 与 逻辑与 等)
           | "(" expr ")" | IDENT | INT | "true" | "false"
           | IDENT "(" [expr ("," expr)*] ")"
           | ("-" | "!") expr
```

运算符与优先级（从低到高）：

| 优先级 | 运算符 | 语义 / 结果类型 |
|---|---|---|
| 1 | `\|\|` | 短路或（bool） |
| 2 | `&&` | 短路与（bool） |
| 3 | `==` `!=` | 同类型相等，结果 bool |
| 4 | `<` `<=` `>` `>=` | int 比较，结果 bool |
| 5 | `+` `-` | int 二元 |
| 6 | `*` `/` | int 二元（`/` 是向零取整，除零为运行期错误） |
| 一元 | `-` `!` | int 取负 / bool 取反 |

语义要点：

* 整数为任意精度 Python int；布尔 `true`/`false`。
* **先声明后使用**；声明时允许不初始化，但在某条控制流路径上未初始化就读取，
  由字节码验证器以 `local.uninit` 拒绝（经典 Java/JVM 风格的确定赋值规则）。
* 函数必须有返回类型；`void` 函数可走到末尾（编译器补隐式 `RET`），
  非 `void` 函数所有路径都必须 `return` 值，否则验证器报 `return.missing`。
* 内建函数（保留名）：`print_int(int)`、`print_bool(bool)`，均返回 void。
* 可运行模块需要无参 `main()`；支持递归与前向引用。
* 注释：`//` 行注释、`/* ... */` 块注释。

---

## 3. 栈式字节码

### 3.1 二进制模块格式

小端序，魔数 `BV 01 00`：

```
magic   4B  = 42 56 01 00
count   1B  函数数 (1..255)
每个函数:
  namelen 1B, name UTF-8
  ret_tag 1B (1=int, 2=bool, 3=void)
  nparams 1B, params  nparams*1B 形参类型标签
  nlocals 1B, locals  nlocals*1B 局部量声明类型标签（槽位接在形参之后）
  max_stack 1B
  codelen 2B, code codelen 字节
  nconsts 2B, 常量池每项 = 1B 标签 + (int: 8B 有符号 | bool: 1B)
```

### 3.2 指令集

所有立即数（常量/槽位/调用编号/跳转偏移）均为 **2B 无符号小端**；
跳转偏移是相对函数代码段起点的**字节偏移**。

| 操作码 | 助记符 | 操作数 | 栈效果 |
|---|---|---|---|
| 0x10 | LOAD_CONST | const# | → value |
| 0x11 | LOAD_LOCAL | slot# | → value |
| 0x12 | STORE_LOCAL | slot# | value → |
| 0x13 | POP | | value → |
| 0x20–0x23 | ADD/SUB/MUL/DIV | | int,int→int |
| 0x24/0x25 | NEG/NOT | | int→int / bool→bool |
| 0x30/0x31 | EQ/NE | | T,T→bool（T 相同） |
| 0x32–0x35 | LT/GT/LE/GE | | int,int→bool |
| 0x40 | JMP | target | 无条件跳转 |
| 0x41 | JIF | target | bool→（弹出；值为**假**跳到 target） |
| 0x50 | CALL | func# | 参数按签名弹出，压返回值（void 不压） |
| 0x51 | RETV | | 弹出 1 个与返回类型一致的值并返回 |
| 0x52 | RET | | void 返回（栈必须为空） |

调用编号 `0..N-1` 是模块内用户函数；内建 `print_int=0xFFF0`、
`print_bool=0xFFF1`。

反汇编示例（`fact` 片段）：

```
func fact -> int params=['int'] locals=[] max_stack=3
     0: LOAD_LOCAL  0  ; slot 0 (n)
     3: LOAD_CONST  0  ; 0 = 2
     6: LT
     7: JIF         14 -> 14
    10: LOAD_CONST  1  ; 1 = 1
    13: RETV
    14: ...
```

---

## 4. 验证器做了什么（核心）

对每个函数做一次基于**抽象状态**（操作数栈类型序列 + 每槽局部量状态：
int/bool/未初始化 BOTTOM）的 worklist 数据流不动点。检查项：

1. **结构 / 跳转边界**
   * 操作码合法、操作数不被截断；
   * 头部声明 `codelen` 与指令自然布局完全一致（防代码段截断/填充变异）；
   * 跳转目标在代码段内（`jump.oob`），且落在指令边界（`jump.misaligned`）；
     **回边（目标 ≤ 当前 pc）与前向边统一检查**；允许跳到“函数末尾”，
     但非 void 函数必须在末尾前给返回值；
   * 局部槽/常量池/调用编号下标在界内。
2. **栈高度**：每条指令入口深度 ≥ 所需操作数（`stack.underflow`），
   压栈不超过声明 `max_stack`（`stack.overflow`）；
   合流点栈高度必须一致（`stack.height`）。
3. **类型 / 类型合流**：二元/一元/分支条件/实参/返回值的类型检查；
   合流点栈上逐槽类型必须相同（`type.merge`）；
   局部量写入类型须与声明一致（`type.local`）。
4. **局部量确定赋值**：`LOAD_LOCAL` 时，若到达该点的**任一路径**上槽位
   未初始化，报 `local.uninit`（合流采用保守规则：任一路径 BOTTOM 即 BOTTOM；
   参数恒已初始化）。
5. **终结性**：任何函数都不允许控制流穿过代码段末尾；非 void 缺返回值是
   `return.missing`（异常返回）。

### 健全性（soundness）结论

通过验证的函数，解释器不做任何下溢/未初始化/类型检查也不会越界：
每一条会弹栈的指令在抽象执行时都已校验深度与类型，跳转 pc 都是合法指令
边界，`CALL` 的实参数量/类型已校验。解释器额外保留的 `pc.invalid.internal`
等分支属于**纵深防御**，正常不可达；变异活动里它们从不出现（见 §7）。

### 最短可读错误路径

验证器在函数 CFG 上用 **Dijkstra**（普通边代价 1、回边代价 5，优先给出
不绕循环的解释）求入口 `pc=0` 到错误指令的最短路径，把每步的字节码偏移
映射回源码行，渲染成：

```
[verifier] local.uninit: 读取局部量 'x'，但存在未对其初始化即到达此处的路径
  函数: main offset=18
  位置: examples/bad_uninit.vl:8:15
   8 |     print_int(x);
                     ^
  [最短错误路径] 函数 main，共 5 步:
  入口  @行 4: LOAD_CONST 0
  -> 顺序执行 @行 5: LOAD_LOCAL 1
  -> 条件跳转 @行 8: LOAD_LOCAL 0
  说明: 控制流意义上的最短可达路径，不代表具体运行时输入。
```

无源码信息的变异模块则显示字节偏移与反汇编文本。

---

## 5. 用法

仅需 Python 3.10+（标准库，无第三方依赖）。

```bash
# 编译（源码 -> .bv 二进制模块）
python -m byteverifier compile examples/hello.vl -o hello.bv
# 验证模块
python -m byteverifier verify hello.bv
# 编译+验证+运行
python -m byteverifier run examples/hello.vl
# 反汇编（源码或 .bv 均可）
python -m byteverifier disasm examples/hello.vl
# 单字节变异后验证（--bit K 翻转第 K 位，或 --value V 整字节替换）
python -m byteverifier mutate hello.bv --byte 40 --value 255
# 三类代表性变异（回边 / 越界跳转 / 异常返回），全部单字节
python -m byteverifier mutation-demo examples/hello.vl
# JSON 服务
python -m byteverifier serve --host 127.0.0.1 --port 8080
```

### HTTP API

| 方法/路径 | 请求 | 响应 |
|---|---|---|
| GET `/health` | – | `{ok:true,...}` |
| POST `/compile` | `{source}` | `{ok, functions, disassembly, module_hex}` |
| POST `/verify` | `{module_hex}` | `{ok, functions}` 或 `{ok:false,error}` |
| POST `/run` | `{source, fuel?}` | `{ok, result, steps, output}` |
| POST `/mutate` | `{module_hex, byte, bit? \| value?}` | `{decode_ok, verify_ok, run_ok, error?}` |

业务错误统一 HTTP 200 + `{"ok":false,"error":{phase,kind,message,
location?,offset?,shortest_path?}}`；坏 JSON/超大包/错误路径才是 4xx。

---

## 6. 自动化测试

```bash
python -m unittest discover -s tests -v
```

60 个测试，覆盖：词法/语法（位置、优先级、注释、语法错误）、合法程序
（循环求和、递归斐波那契、短路布尔、提前返回、void 调用、二进制往返、
fuel/深度资源限制、除零）、非法程序（未初始化、缺返回、值返回/无值返回、
int+bool、条件非 bool、写错类型、实参类型不符）、**手工构造字节码**
（越界/非对齐跳转、合法回边、合流栈高度不一致、合流类型不一致、
槽/常量越界、max_stack 过小、RETV 栈残留、未知调用编号）、变异活动
健全性，以及 HTTP 服务端到端。

---

## 7. 变异活动与如实运行记录

以下命令与输出均在本环境（Linux, Python 3.12.3）实际运行得到。

### 7.1 合法程序运行

```text
$ python -m byteverifier run examples/hello.vl
15
720
true
[main 返回 None，146 步]
```

### 7.2 三类必测变异（均为单字节扰动）

```text
$ python -m byteverifier mutation-demo examples/hello.vl
变异1 [回边重定向] main: 跳转目标低字节 -> 0（改动 1 字节）
  -> 验证通过（语义改变，回边本身合法）
变异2 [越界跳转] main: offset=19 跳转目标 -> 256（改动 1 字节）
  -> [verifier/jump.oob] 跳转到 offset=301，但代码段长度只有 77（越界跳转）
  （附入口到该 JIF 的最短路径，8 步）
变异3 [异常返回] fact: RETV -> RET（改动 1 字节）
  -> [verifier/return.value] 返回类型为 int 的函数不能用无值 RET
  （附最短路径，6 步）
```

### 7.3 穷举单 bit 翻转（1632 个变异体）+ 定向变异

```text
$ python scripts/mutation_campaign.py examples/hello.vl
模块大小: 204 字节，函数: ['fact', 'main']

=== 单 bit 翻转穷举（共 1632 个变异体）===
     81  decode:const.tag
      2  decode:func.name
     11  decode:func.name.utf8
     32  decode:magic
      1  decode:module.funcs
    254  decode:opcode
      2  decode:trailing.bytes
     46  decode:truncated
     87  decode:type.tag
     28  ran:no-main
    416  ran:ok
     62  runtime:depth.exceeded
     64  runtime:fuel.exhausted
     79  verify:call.oob
    132  verify:const.oob
     13  verify:jump.misaligned
     34  verify:jump.oob
    231  verify:local.oob
     10  verify:local.uninit
      2  verify:return.value
      6  verify:stack.height
      1  verify:stack.leftover
      9  verify:stack.overflow
     11  verify:stack.underflow
      6  verify:type.arg
      7  verify:type.local
      5  verify:type.operand

=== 定向跳转立即数替换（每跳转 x 高低字节 x 0x00/0xFF）===
      3  ran:ok
      3  runtime:fuel.exhausted
      6  verify:jump.oob

=== 定向返回指令替换 RETV<->RET ===
      2  verify:return.value
      1  verify:stack.underflow

健全性（解释器不发生栈下溢/内部错误）: 通过 ✅
```

解读：变异体在**解码 / 验证 / 带检查的运行期资源错误（fuel、递归深度）**
三个环节被全部拦截；`ran:ok` 是变异后语义仍然自洽的程序（例如改了常量值）。
**没有任何变异体触发解释器的内部错误或栈下溢**——这是验证器健全性的
经验证据。

### 7.4 开发中实际发现并修复的问题（如实记录）

1. **验证器真漏洞（变异活动逼出）**：把函数头部 `codelen` 改小（截断）后，
   解码器只返回“自然布局的前缀指令”，而旧验证器信任头部长度且允许跳到
   “代码末尾”，导致一个变异体在解释器里 `pc.invalid.internal`。
   修复：验证器比对“声明 codelen == 指令自然布局长度”，跳转边界以自然
   布局为准，并要求所有函数都必须以 RET/RETV 显式结束（void 的隐式 RET
   由编译器保证）。修复后该类变异以 `code.length` / `jump.oob` /
   `ret.missing` 被拒绝，全量回归通过。
2. `a || b` 短路代码生成方向写反（错误地用“JIF=假则跳”实现“左真则短路”），
   由 `test_bool_logic` 捕获并修正。
3. `return` 之后块结束标签悬空导致编译器 `IndexError`（bad_return 示例）；
   引入“代码末尾虚拟位置”，并由验证器按返回类型裁决其合法性。
4. 解码变异产生的非法 UTF-8 函数名曾逃逸为 `UnicodeDecodeError`，
   已包装为 `decode:func.name.utf8`。

### 7.5 已知限制

* 局部量为**函数级作用域**（无块级作用域）；无 `break/continue`、字符串、
  数组与结构体；整数为 Python 任意精度（无溢出语义）。
* 错误“最短路径”是控制流可达意义下的最短，不做路径条件可满足性分析
  （即不证明存在某个具体输入能触发该路径）。
* 纯后端：不含任何前端页面。

---

## 8. 请求样例

见 `examples/requests/*.http`（可直接用 curl 复现的请求/响应样例）。
