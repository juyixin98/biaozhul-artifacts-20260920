# SSA 构造与回退（纯后端工具链）

从零实现的小语言**纯后端**工具链：自定义词法/语法 → 整数 IR（含分支与循环）→
**支配关系 → 插入 φ → 支配树重命名**（Cytron 风格 SSA 构造）→
**关键边分裂 + 并行复制（含交换环）串行化** 的 φ 消除 → 无 φ 可执行 IR。
同一个解释器可以执行原始 IR、SSA IR 与回退后 IR，三种形态逐程序对照结果。

* 只用 Python 3.10+ 标准库；**不调用任何现成编译器/编译器库**
  （不依赖 llvmlite、pycparser、networkx 等），词法、语法、支配分析、
  SSA 构造、φ 消除、解释器全部手写。
* **不做前端**：没有页面/UI；交付物为 Python 库、CLI、JSON HTTP 服务。
* 所有 AST 与 IR 节点保留**源码位置**（文件/行/列/偏移），
  运行时除零等错误可回溯到源行。

## 目录

```
ssa_tool/
  lexer.py              词法分析（Token + 行列/偏移）
  ast_nodes.py          AST 定义（全部带 Span）
  parser.py             递归下降语法分析 + 作用域检查
  ir.py                 IR 数据结构（三种形态统一表示）+ 文本打印/JSON
  ir_builder.py         AST -> 原始 IR（alloc/load/store + 控制流）
  dominance.py          可达性/RPO/idom(Cooper 迭代)/支配树/支配边界
  ssa_construction.py  φ 插入 + 支配树重命名（含不可达块隔离处理）
  ssa_validate.py       SSA 合法性：单一定义 / def-dom-use / φ 入边
  parallel_copy.py      并行复制串行化（ready-set + 临时量破环）
  phi_elimination.py    φ 消除：位置化 + 关键边分裂 + 边复制串行化
  interpreter.py        三形态统一解释器（截断向零整数语义）
  pipeline.py           端到端编排 + AST JSON
  cli.py                命令行
  service.py            JSON HTTP 服务（http.server）
examples/               样例源码、请求样例、手工 φ 环脚本
tests/                  unittest 自动化测试（无第三方依赖）
run_tests.sh            一键测试脚本
RUN_REPORT.md           实际运行命令与结果记录
```

## 语言参考（ToyLang，自定义语法）

单入口、单函数的命令式整数语言（无函数调用，专注控制流/SSA）：

```
program := "func" "main" "(" [ID ("," ID)*] ")" block
block   := "{" stmt* "}"
stmt    := "var" ID ["=" expr] ";"
          | ID "=" expr ";"
          | "if" "(" expr ")" block ("else" block)?
          | "while" "(" expr ")" block
          | "return" [expr] ";"
          | expr ";"
expr    := or
or      := and ("||" and)*
and     := eq ("&&" eq)*
eq      := rel (("==" | "!=") rel)*
rel     := add (("<" | "<=" | ">" | ">=") add)*
add     := mul (("+" | "-") mul)*
mul     := unary (("*" | "/" | "%") unary)*
unary   := ("-" | "!") unary | primary
primary := INT | "true" | "false" | ID | "(" expr ")"
```

* 整数为任意精度整数；`true/false` 分别为 `1/0`；
* `/`、`%` **截断向零**（C 语义）：`-17/5 == -3`、`-17%5 == -2`，除零报错；
* `&&`、`||` 短路求值（带源码位置）；
* 变量须先 `var` 声明（支持参数同名），禁止重复声明/未声明使用；
* `var x;` 默认初值 0；`var` 初值在语句执行到时写入，
  因此循环体内的 `var j = 0;` 每次迭代都会重置（这是真实语义，
  早期版本漏发这条 store 曾导致嵌套循环计数错误，已被测试固化）；
* 注释：`// 行注释` 与 `/* 块注释 */`。

## IR 三阶段

### 1) 原始 IR（raw，内存风格）

每个源变量一个槽，统一在入口 `alloc` 初值 0；参数 `param` 后 `store`；
表达式用临时寄存器；`if`/`while` 显式化为 `br/jmp`。终结之后的语句放进
`dead.N` 块（`unreachable` 终结），因此前端层就会产生不可达块。

```
%x = alloc 0
%t = param 0
store %t -> %x
br %cond, @then.0, @else.2
...
```

操作码：`const param alloc load store copy add sub mul div mod
lt le gt ge eq ne neg lnot`，终结 `jmp br ret unreachable`。

### 2) SSA IR

* 支配分析在**入口可达子图**上计算（RPO + Cooper-Harvey-Waterman
  idom 迭代 + Cytron 支配边界）；
* φ 只插到可达块的支配边界；
* 重命名沿支配树 DFS，每槽维护版本栈。**每次 store 生成全新 SSA 名字**
  （一条 `copy` 定义），load 取最近版本；φ 入边填**槽版本名本身**
  （而非它 copy 的静态源），保证循环携带/交换语义跨迭代正确；
* 不可达块不插 φ、不入重命名 DFS，单独隔离处理：外部引用落地为独立
  `const 0`，临时值改名 `u<N>`，保证全函数仍满足“每个名字一处定义”；
  可达 φ 块来自不可达前驱的入边补 `const 0`。

### 3) φ 消除 -> 可执行 IR（exec）

标准两招，缺一不可：

1. **φ 目的位置化**：SSA 值不可变，而 φ 的值随进入边变化。为每个 φ 目的
   分配可写寄存器 `r<N>`，全函数把对 φ 值的使用改指向 `r<N>`，
   边上复制写入这些寄存器；
2. **关键边分裂**：边 `p -> b`（b 有 φ）且 p 出度>1、b 入度>1 时，
   插入 `@split.N`（复制移入新块，避免污染 p 的另一后继）；
3. **并行复制串行化**：每条边上 `r1<-v1, r2<-v2, ...` 是并行语义。
   ready-set 算法发射无依赖复制；出现环（如循环 latch 上
   `ra<-rb, rb<-ra`）时用临时量保护并改写旧值读取，再回 ready-set。
   实现通过 100 万组随机并行复制的等价性测试。

## SSA 校验（验收“每个 SSA 值单一定义”）

`ssa_validate.py` 检查：

1. **单一定义**：每个名字全函数恰有一处定义（φ/const/算子/param/copy）；
2. **def-dom-use**：定义块支配使用块；同块时要求先定义后使用；
3. **φ 入边完整**：对每个前驱恰有一条入边，且入边值在该前驱出口可用
   （定义支配前驱）；
4. 结构：无残留 `alloc/load/store`，所有块有终结指令且跳转闭合。

## 三种形态对照执行

`interpreter.py` 同一份代码支持三种 flavor（raw 走内存槽，ssa/exec 走寄存器，
φ 在进入块时按前驱读取）。CLI `run` 对相同参数执行三遍并比对返回值
（SSA 去除了 load/store，步数更少属正常）。

## CLI

```bash
python3 -m ssa_tool 源码.toy parse     # AST(JSON)
python3 -m ssa_tool 源码.toy raw       # 原始 IR 文本
python3 -m ssa_tool 源码.toy ssa       # SSA IR 文本
python3 -m ssa_tool 源码.toy exec      # φ 消除后可执行 IR
python3 -m ssa_tool 源码.toy check     # 仅做 SSA 合法性校验
python3 -m ssa_tool 源码.toy run --args 3,5   # 三形态对照执行
python3 -m ssa_tool 源码.toy pipeline --json   # 全阶段 JSON
```

负数参数请用 `--args=-5`（避免 argparse 视为选项）。

## JSON 服务

```bash
python3 -m ssa_tool.service --host 127.0.0.1 --port 8000
```

`POST` 接口（均返回 JSON；错误返回 `{"ok":false,...}`，HTTP 400）：

| 路由 | 入参 | 说明 |
|---|---|---|
| `/health` | — | 健康检查（GET 亦可） |
| `/parse` | `source, file?` | AST JSON（含 span） |
| `/build_ir` | `source` | 原始 IR（json + text） |
| `/ssa` | `source` | SSA IR + 校验违规列表 |
| `/eliminate` | `source` | φ 消除后可执行 IR |
| `/interpret` | `source/ir, flavor?, args?, trace?` | 执行（raw/ssa/exec） |
| `/pipeline` | `source, args?, run?` | 全流程 + 三形态对照 |

请求样例见 `examples/requests/*.json`，`examples/requests/curl_smoke.sh`
会自动起服务并打全部端点。

## 自动化测试

```bash
bash run_tests.sh                       # unittest + 样例对照 + 手工φ环
python3 -m unittest discover -s tests  # 仅单元测试
```

覆盖：词法位置/注释/错误码；递归下降优先级与作用域；支配关系（菱形/循环/
不可达排除）；SSA 单一定义、φ 形状、不可达块、变异注入检测；并行复制
固定用例 + 50k/100 万随机等价；三形态程序等价（菱形、循环携带、嵌套循环、
嵌套分支、短路、截断向零除法、除零源行、死循环保护、不可达不可执行）；
关键边分裂存在性与 CFG 闭合；HTTP 服务全端点（含 400/404）。

## 设计取舍 / 边界

* 单函数、无调用/堆/指针：把复杂度集中在控制流与 SSA 本身；
* 不做 SSA 之上的优化（不做常量折叠/死代码删除），因此能逐阶段严格对照；
* 不可达块予以保留（而非删除），用于演示支配分析将其排除、φ 不为其插入、
  解释执行保证不可达；
* 解释器有步数上限（默认 100 万）防止 `while(1)` 挂死。
