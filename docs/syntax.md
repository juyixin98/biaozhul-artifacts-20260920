# LattLang 语言参考

LattLang 是为本项目自定义的一门极小命令式整型语言。编译器（词法、语法、
IR、SSA、分析、优化、解释器）全部手写，仅依赖 Python 3 标准库，**不使用任何
现成编译器/解析器生成器或编译器基础设施**。

## 1. 词法

| 类别 | 形式 |
|---|---|
| 整数 | `[0-9]+`（任意精度，无负数记号，负号是一元运算符） |
| 标识符 | `[A-Za-z_][A-Za-z0-9_]*`（编译器生成的临时名以 `$` 开头） |
| 关键字 | `if` `else` `while` `print` `true` `false` |
| 运算符 | `+ - * / %`、`== != < <= > >=`、`&& || !`、`:=` |
| 分隔符 | `( ) { } ;` |
| 注释 | `#` 到行尾 |
| 空白 | 空格、制表符、换行 |

词法器为每个 token 记录 1-based 的行、列与字节偏移（`offset`/`length`）。

## 2. 语法（EBNF）

```ebnf
program    := statement*
statement  := assign | print | if | while
assign     := IDENT ":=" expr [";"]
print      := "print" expr [";"]
if         := "if" expr block ["else" (if | block)]
while      := "while" expr block
block      := "{" statement* "}"
expr       := logicOr
logicOr    := logicAnd ("||" logicAnd)*
logicAnd   := equality ("&&" equality)*
equality   := relational (("==" | "!=") relational)*
relational := additive (("<" | "<=" | ">" | ">=") additive)*
additive   := unary (("+" | "-") unary)*
unary      := ("!" | "-") unary | primary
primary    := INT | "true" | "false" | IDENT | "(" expr ")"
```

赋值语句和 print 语句后的分号可选。`else` 与最近的 `if` 结合。不允许顶层裸
`{ }` 块（块只作为 if/while 的体）。

## 3. 语义

* **类型**：只有一种类型——任意精度整数。`true`/`false` 即 `1`/`0`。
* **变量**：变量无需声明；**首次赋值或读取前的值为 0**。
* **运算**：
  * `+ - *` 常规整数运算。
  * `/`（整除）与 `%`（取模）采用**向零截断**语义（与 C/JS 一致），满足
    `a == (a/b)*b + (a%b)`。例如 `-7/2 == -3`，`-7%2 == -1`。
  * 除数（或模数）为 0 时抛出运行时错误 **“division or modulo by zero”**。
  * 比较运算结果为 `0`/`1`。
  * `&&`、`||` **不短路**：两个操作数都先求值，结果为 `0`/`1`。这一点对
    保持可观察副作用（尤其是除零）至关重要。
  * `!x` 定义为 `x == 0`（0 变 1，非 0 变 0）。
* **控制流**：`if c` 在 `c != 0` 时走 then，否则 else；`while c` 常规循环。
* **输出**：`print e` 将 `e` 的十进制值单独输出到一行。
* 程序正常结束不产生额外输出；运行时错误带**出错运算符的源码位置**。

## 4. 一个程序示例

```
# 0 次循环：循环体不可达
i := 10
while 0 {
  print i
}
print i          # 输出 10
```

## 5. 位置保留

从 AST 到 SSA IR 再到优化后的 IR，每条指令都携带其来源表达式的 `Span`
（起止行列、偏移、长度）。文本 IR 用行尾注释 `#@ 行:列-行:列` 保存位置，
保证 IR 打印/解析往返后错误位置不丢失。运行时错误（除零等）在 AST 解释器、
SSA IR 解释器与优化后 IR 解释器三处报告**完全相同的源码位置**。

## 6. SSA IR 概要（详见 README“IR”一节）

* 单过程，基本块组成的 CFG，块均以 `jmp`/`br`/`ret`/`unreachable` 显式终止。
* 指令：`const`、`copy`、一元（`neg`/`not`）、二元
  （`add sub mul div mod eq ne lt le gt ge and or`）、`print`。
* 经 Cytron 等的最小（pruned）SSA 构造：支配者 → 支配边界 → 插入 φ →
  支配树重命名。每个变量有隐式初值 0（重命名栈深度 0）。
