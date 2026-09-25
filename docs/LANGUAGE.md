# ResFlow 语言规范（版本 1.0.0）

ResFlow 是为“资源释放路径分析”专门设计的一门极小命令式语言。它只包含
分析所需的构造：显式资源操作、分支、循环、异常与函数。本文件是权威语法
与语义说明；工具链（词法、语法、CFG、分析）全部手写，不依赖任何现成
编译器框架。

## 1. 词法

### 1.1 记号

| 类别 | 内容 |
| --- | --- |
| 标识符 | `[A-Za-z_][A-Za-z0-9_]*` |
| 整数 | `[0-9]+`（32 位范围不做强制，按 Python 整数处理） |
| 布尔 | `true`、`false` |
| 字符串 | `"..."`，支持 `\n \t \" \\ \0` |
| 关键字 | `fn let acquire release use return throw if else while try catch true false` |
| 标点 | `( ) { } ; , =` |
| 运算符 | `== != < <= > >= && || + - * / % !`（无单目 `+`） |
| 注释 | `//` 行注释；`/* ... */` 块注释，块注释可嵌套 |

词法单元全部带**半开区间**源码位置 `[offset, end_offset)`，并同时记录
1 起始的 `line`、`column`（按字节列计数，制表符算 1 列）。位置从词法
单元一路保留到 AST、CFG 节点和最终诊断。

### 1.2 空白与分号

语句以 `;` 结束；空语句 `;` 合法。换行不是分隔符。

## 2. 语法（EBNF）

```ebnf
program      = { function } ;
function     = "fn" IDENT "(" [ params ] ")" block ;
params       = IDENT { "," IDENT } ;
block        = "{" { statement } "}" ;

statement    = ";"
             | "let" IDENT [ "=" expr ] ";"
             | IDENT "=" expr ";"
             | resource_op [ "(" ] IDENT [ ")" ] ";"
             | "return" [ expr ] ";"
             | "throw" STRING ";"
             | "if" "(" expr ")" block [ "else" block ]
             | "while" "(" expr ")" block
             | "try" block "catch" "(" IDENT ")" block
             ;
resource_op  = "acquire" | "release" | "use" ;

expr         = logic_or ;
logic_or     = logic_and { "||" logic_and } ;
logic_and    = equality  { "&&" equality } ;
equality     = comparison { ( "==" | "!=" ) comparison } ;
comparison   = additive  { ( "<" | "<=" | ">" | ">=" ) additive } ;
additive     = term      { ( "+" | "-" ) term } ;
term         = unary     { ( "*" | "/" | "%" ) unary } ;
unary        = ( "!" | "-" ) unary | primary ;
primary      = INT | "true" | "false" | STRING | IDENT | "(" expr ")" ;
```

资源语句同时接受 `acquire(r);` 与 `acquire r;` 两种写法（语法糖等价）。

## 3. 程序结构

* 一个程序由一个或多个**函数**组成；函数名不可重复，函数当前相互独立
  （无调用表达式），每个函数单独做路径分析。
* 函数至少要有一个函数定义，否则词法/语法阶段报错。
* 函数没有返回值类型；`return [expr];` 是唯一的正常出口，隐式走到闭花
  括号也视为正常出口（CFG 上汇入 `exit` 节点）。

## 4. 资源模型（核心语义）

### 4.1 资源名字的引入

一个标识符属于“资源宇宙”当且仅当满足其一：

1. 它是函数**形参**——表示所有权在入口处传入，初始状态为 `held`；
2. 它出现在某条 `acquire` / `release` / `use` 语句中。

仅出现在表达式（通常是条件）里的标识符（如 `let flag;` 后在 `if (flag)`
中读取）是**符号输入**：无法静态求值时，分析器对 true / false 两边都做
探索；它不进入资源宇宙，也不参与泄漏检查。

`let x;`（无初始化式）声明一个值未知的符号量；`let x = expr;` 与
`x = expr;` 会被 CFG 当作不改变资源状态的普通节点（用于承载条件常量
折叠，见 6.2）。

### 4.2 生命周期状态

```
                 acquire                release
 unacquired ───────────────▶ held ───────────────▶ released
     ▲                          │                    │
     │        (重新获取合法)     │ acquire            │ release (重复释放, 报错)
     └──────────────────────────┘                    │
                                                     ▼
                                                (保持 released)
```

* `acquire`：`unacquired/released → held`；`held → held` 并报
  `double_acquire`（上一实例泄漏）。
* `release`：`held → released`；`released → released` 并报
  `double_release`；`unacquired → released` 并报 `release_unacquired`。
* `use`：不改变状态；`released` 上报 `use_after_release`，
  `unacquired` 上报 `use_unacquired`。
* 在函数的 `exit` 或 `uncaught` 出口仍为 `held` 的资源报
  `resource_leak`。形参同样必须在出口前释放。
* 释放之后允许重新 `acquire`（状态机合法迁移），不算缺陷。

## 5. 控制流与异常

### 5.1 分支

`if (c) { ... } [else { ... }]`。无 `else` 时 CFG 显式补一条 false 边到
汇合（merge）节点，使“什么都不做”的路径在图上可见。

### 5.2 循环

`while (c) { ... }`。循环节点有 true（进入循环体）、false（退出）两条
出边，循环体末尾有一条 `back` 边回到循环头。分析器对循环做**有界展开**
（见 6.3），保证终止。

### 5.3 异常（异常边是一等 CFG 边）

`throw "message";` 不产生普通后继，而产生且只产生一条 `exception` 边：

* 若词法上处在某个 `try` 块内（含嵌套内层 try），异常边连到最近的
  `catch_head`，边上标记 `caught: true`；
* 否则连到函数级 `uncaught` 出口节点，标记 `caught: false`。

`catch (e) { ... }` 的 `catch_head` **只能**经由异常边进入；其处理块内
再次 `throw` 会按同样规则继续向外路由（内层 catch 中 rethrow → 外层
catch；最外层 → `uncaught`）。异常边与普通边在分析器中使用同一套状态
传播逻辑，因此异常出口上的资源状态会被真实计算（用于发现异常路径泄漏）。

## 6. 分析

### 6.1 路径敏感的有界符号执行

分析器在 CFG 上做确定性深度优先遍历，每条被探索的路径维护
`resource → state` 映射：

* 在 `branch` 节点按 true / false 分叉；
* 在 `loop` 节点按进入 / 退出分叉；
* `throw` 沿异常边继续，`return` 沿普通边进入 `exit`；
* 到达 `exit` / `uncaught` 时对残留 `held` 资源报泄漏。

每条路径输出：`node_trace`（节点轨迹）、`decisions`（每个分支/循环判定
及其取值、轮次）、`state_changes`（每次资源操作的 before→after）、
`final_states`、路径级诊断。

### 6.2 常量折叠与不可行边剪枝

只由字面量与纯运算符组成的条件在编译期折叠（`1 < 2` → true，
`false` → false，支持算术/比较/逻辑及括号）。折叠为常量时只走可行的那
一条边，并在 decision 上标记 `"constant": true`；引用变量的条件无法
折叠，两边都探索。常量折叠只影响可行性，不改变 4.2 的状态机。

### 6.3 循环展开界与截断

`loop_bound`（默认 2，可经 CLI / API 调整）是每条路径上**进入**循环体
的次数上限：

* 到达循环头且本路径已进入 `loop_bound` 次时：
  - 若条件可折叠为 true（无出口），产出一条
    `status: "loop_truncated"` 的路径后停止该方向；
  - 若条件未知，仍探索 false 出口，同时额外产出一条截断路径，其决策里
    带 `"truncated": true`，`iteration` 是未能进入的那一轮编号。
* 因此 `while(true){}` 不会使分析发散。

此外有全局 `max_paths`（默认 512）预算；超限时产出一条
`status: "path_cap"` 的合成路径。

### 6.4 可复现性

后继按固定边序遍历（`true < false < normal < exception < back`），字典
序输出稳定；对同一源码重复分析产生字节级一致的 JSON。路径 id 从 0 连
续编号。

## 7. 诊断代码

| code | 触发条件 |
| --- | --- |
| `double_acquire` | `held` 状态再次 acquire（旧实例泄漏） |
| `double_release` | `released` 状态再次 release |
| `release_unacquired` | 对 `unacquired` 资源 release |
| `use_after_release` | `released` 状态 use |
| `use_unacquired` | `unacquired` 状态 use |
| `resource_leak` | `exit`/`uncaught` 出口资源仍 `held` |

每条聚合诊断给出：源码位置（行列+偏移）、涉及资源、出现该问题的
`path_ids` 与终点类型 `terminals`（`exit` / `uncaught`）。

## 8. 一个完整例子

```
fn exceptional() {
  let flag;            // 符号输入：两条分支都探索
  acquire(db);
  use(db);
  if (flag) {
    throw "backend refused";   // 异常边 -> uncaught，db 泄漏
  }
  release(db);
  return;
}
```

分析得到两条路径：true 路径终点 `uncaught`、`db = held`（报泄漏）；
false 路径终点 `exit`、`db = released`（无诊断）。
