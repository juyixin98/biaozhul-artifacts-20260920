# Slang 小语言规范（LANGUAGE）

Slang 是为本项目自定义的、用于演示栈式字节码验证的命令式小语言。
只有两种标量类型：`int`（有符号整数，立即数为 16 位范围
`[-32768, 32767]`，运行期为任意精度整数）与 `bool`（`true`/`false`）。
**没有隐式类型转换**：`int` 与 `bool` 不能混用。

## 1. 词法

- 标识符：`[A-Za-z_][A-Za-z0-9_]*`
- 整数字面量：`[0-9]+`
- 关键字：`fn var if else while return print true false`
- 类型名 `int` / `bool` 是*上下文关键字*：词法上仍是标识符，
  只在声明位置（参数、`int x;`）被当作类型。
- 注释：`//` 行注释；`/* ... */` 块注释（不嵌套）。
- 标点与运算符：

  ```
  ( ) { } , : ;
  =  ==  !=  <  <=  >  >=
  +  -  *  /  %
  !  &&  ||
  ```

## 2. 语法（EBNF）

```ebnf
program    = { function } ;

function   = "fn" IDENT "(" [ param { "," param } ] ")"
             [ ":" type ] block ;

param      = type IDENT ;
type       = "int" | "bool" ;

block      = "{" { statement } "}" ;

statement  = ";"
           | "var" IDENT "=" expr ";"
           | type IDENT [ "=" expr ] ";"
           | IDENT "=" expr ";"
           | "if" "(" expr ")" block [ "else" block ]
           | "while" "(" expr ")" block
           | "return" [ expr ] ";"
           | "print" expr ";"
           | expr ";"
           ;

expr       = logic_or ;
logic_or   = logic_and { "||" logic_and } ;
logic_and  = equality  { "&&" equality } ;
equality   = relation  { ("==" | "!=") relation } ;
relation   = additive  { ("<" | "<=" | ">" | ">=") additive } ;
additive   = term      { ("+" | "-") term } ;
term       = unary     { ("*" | "/" | "%") unary } ;
unary      = ("-" | "!") unary | primary ;
primary    = INT_LIT | "true" | "false"
           | IDENT [ "(" [ expr { "," expr } ] ")" ]
           | "(" expr ")" ;
```

运算符一律左结合；优先级从低到高为：

```
||  <  &&  <  == !=  <  < <= > >=  <  + -  <  * / %  <  一元 - !  <  基本式
```

## 3. 程序结构与语义要点

- 一个程序由若干顶层函数组成；要被解释器执行的模块必须含
  无参函数 `main`（`fn main() { ... }`）。
- 函数可以前向引用、相互递归。
- 返回类型写在 `): 类型` 之后；不写表示 `void`。
  - `return;` 只能用于 `void` 函数；
  - `return e;` 只能用于带返回类型的函数，且 `e` 类型必须一致；
  - 函数体在结构上以返回指令收尾（编译器补尾，验证器检查
    “不得从末尾落入虚无”）。
- 变量：
  - `var x = e;` 类型由 `e` 推断，必须立即初始化；
  - `int x;` / `bool b;` 可以不带初始化式，此时槽位为
    *未初始化*，**在某条控制流路径可能未赋值时读取会被验证器拒绝**
    （错误码 `LOCAL_UNINITIALIZED`，采用确定赋值/合流即未初始化的
    保守规则，与 JVM 的思想一致）。
  - 变量作用域为整个函数体（简化设计），不允许重复声明。
- `print e;` 弹出并打印一个 `int` 或 `bool`（bool 输出小写
  `true`/`false`），无返回值。
- 整数除法 `/` 与取模 `%` 采用**向零截断**语义：
  `-7 / 2 == -3`，`-7 % 2 == -1`。除零/对零取模是运行期错误。
- 比较与逻辑运算产生 `bool`；逻辑运算按内置运算实现（无条件短路
  字节码，两侧都会求值）。

## 4. 示例

```slang
fn gcd(int a, int b): int {
    while (b != 0) {
        int t = a % b;
        a = b;
        b = t;
    }
    return a;
}

fn main() {
    print gcd(48, 18);     // 6
}
```

更多示例见 `examples/`：`sum_loop.sl`（回边）、`early_return.sl`
（分支提前返回/递归）、`bool_logic.sl`（bool 与合流确定赋值）、
`uninit_read.sl`（未初始化检查的变异靶点）。

## 5. 前端错误

词法/语法/编译期（类型）错误由编译器以 `CompileError` 给出，
携带源码行、列与源代码片段。JSON 服务在 `stage: "compile"` 的
响应中返回这些位置；CLI 直接渲染带指示符 `^` 的诊断。
