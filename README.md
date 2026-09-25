# TaintLang — 跨函数污点传播分析（纯后端）

一个从零实现的小型命令式语言工具链：**手写词法分析器、手写递归下降语法分析器、
自定义寄存器/基本块 IR，以及基于有限调用串上下文（k-CFA 风格）摘要的过程间
may-taint 数据流分析**。分析结果通过零第三方依赖的 JSON/HTTP 服务对外提供。

> **非目标 / 诚实声明**：本项目不解析任何真实语言，不使用现成编译器前端
> （无 ANTLR / lark / pycparser / tree-sitter / LLVM），也**不声称对任意图灵完备
> 语言的污点分析是完备（sound-and-complete）的**。这是一个在**自定义玩具语言**
> 上、语义明确、终止性有界的静态分析器；它包含若干**刻意的、已记录的保守近似**，
> 会产生**已知的误报**（见“保守近似与已知误报”一节）。

---

## 1. 目录结构

```
taintlang/
  location.py   源码位置（offset + 1-based 行列，span 保留源码切片）
  errors.py     LexError / ParseError / BuildError / ConfigError
  config.py     分析配置（source/sink/sanitizer 名称、上下文深度 k 等）
  lexer.py      手工词法分析器（token + span）
  ast_nodes.py  AST 节点定义
  parser.py     手工递归下降 / Pratt 优先级爬升解析器
  ir.py         自定义 IR：寄存器指令、基本块、终结符（含跨过程调用边）
  builder.py    AST -> IR 降级（调用强制拆块，调用边显式化）
  analyzer.py   过程间污点分析（有限上下文摘要 + worklist 不动点 + 流图）
  pipeline.py   源文本 -> ... -> JSON 结果 的一站式门面
  service.py    仅用标准库 http.server 的 JSON/HTTP 服务
  cli.py        命令行：analyze / serve
examples/
  programs/*.tl     验收样例程序（每个文件头注释写明预期告警）
  requests/*.json   /analyze 请求样例与真实响应
tests/              标准库 unittest 自动化测试（73 个用例）
RUNLOG.md           实际运行命令与结果的如实记录
```

## 2. 运行环境与运行方式

- Python 3.10+（开发与验证使用 Python 3.12，标准库即可，无任何第三方依赖）。

```bash
# CLI：直接分析文件
python3 -m taintlang.cli analyze examples/programs/cross_function.tl \
    --entry main --summary

# 输出完整 JSON（含 IR 转储）
python3 -m taintlang.cli analyze examples/programs/basic.tl

# JSON/HTTP 服务
python3 -m taintlang.cli serve --host 127.0.0.1 --port 8080

# 自动化测试
python3 -m unittest discover -s tests -v
```

`--entry` 默认取程序中**第一个声明的函数**；样例统一显式使用 `main`。
默认把入口函数的形参当作攻击者可控的污点来源（可通过
`--no-tainted-entry-params` 或请求字段 `tainted_entry_params:false` 关闭）。

## 3. 语言语法（自定义并文档化）

TaintLang 是无类、无堆、无指针的整数/布尔/字符串命令式语言，函数单一返回值。
注释为 `// ...` 与 `/* ... */`。

```
program          := func+
func             := 'func' IDENT '(' params? ')' block
params           := IDENT (',' IDENT)*
block            := '{' stmt* '}'

stmt             := 'var' IDENT ('=' expr)? ';'
                   | IDENT '=' expr ';'                 # 变量须先用 var 声明
                   | 'if' '(' expr ')' block ('else' block)?
                   | 'while' '(' expr ')' block
                   | 'return' expr? ';'
                   | expr ';'

expr （优先级从低到高）:
  or             := and ('||' and)*
  and            := equality ('&&' equality)*
  equality       := relational (('==' | '!=') relational)*
  relational     := additive (('<' | '<=' | '>' | '>=') additive)*
  additive       := multiplicative (('+' | '-') multiplicative)*
  multiplicative := unary (('*' | '/' | '%') unary)*
  unary          := ('!' | '-') unary | call
  call           := primary ('(' args? ')')?           # 不支持链式/方法调用
  primary        := NUMBER | STRING | 'true' | 'false' | IDENT | '(' expr ')'
```

语义约定（分析所需的最小语义）：

- 整数/布尔/字符串为值；二元/一元运算在任一操作数带污点时结果带污点
  （字符串拼接等不做区分，统一按污点传播处理）。
- 变量为函数局部；按值传参；无全局、无引用别名、无栈上取址。
- 三个**标记调用**（名称可配置）是污点模型的核心原语：
  - `source()`：产生一个全新的污点来源；
  - `sink(x)`：污点到达点，若实参可能带污点则产生一条告警；
  - `sanitize(x)`：**强清洗器**，返回值绝不继承 `x` 的污点（不重新污染）。
- 调用一个程序内**没有函数体**的名字（如外部库 `log(...)`）是**不透明调用**。

## 4. 自定义 IR

见 `taintlang/ir.py`。要点：

- 值流经**寄存器**：源变量保持原名，综合临时寄存器名为 `%0, %1, ...`；
  指令 UID 与调用点 UID 在**整个程序内全局唯一**。
- 函数是 `Block` 图，块之间只经终结符连接，无 fall-through、无 phi：
  `Jmp` / `Br(cond,then,else)` / `Ret(value?)` / `CallTerm(...)`。
- **调用是终结符**：`CallTerm` 指向被调函数入口，并带一个显式的**继续块**
  `cont`；这让“调用边 / 返回边”成为过程间 CFG 上的一等边，而不是块内副作用。
  表达式中任何位置出现用户函数调用（含嵌套调用）都会强制当前块分裂。
- 三个污点原语降级为一等指令 `SourceInstr / SanitizeInstr / SinkInstr`；
  不透明调用降级为 `UnknownCall`。
- 每条指令、每个块、每个终结符都保留源码 `Span`（offset、1-based 行列、
  源码切片文本），因此最终告警路径能渲染回源码位置。

## 5. 分析算法

入口实现：`taintlang/analyzer.py`。

1. **数据流域**：环境 `寄存器 -> 污点来源集合（origin set）`。
   合流取集合并；这是 **may 分析、块内流敏感**。默认**无强更新**
   （一个曾经赋过污点的单元在后续合流中保留污点），因此整体偏向不漏报。
2. **有限调用串上下文（call-string / k-CFA 风格）**：上下文是调用栈上
   最近 `k` 个调用点 UID。`k=0` 时对同一函数的所有调用合并（0-CFA）；
   `k=null` 不受深度限制，但由 `max_call_sites` 硬性封顶。
   **“有限上下文摘要”**即：每个 `(函数, 上下文)` 的“入口形参污点 →
   返回值污点/函数内 sink”关系，就是该函数在有界上下文下的摘要；
   这些关系通过一个**全局、单调的 worklist 不动点**一并求解，无需
   预计算过程摘要，也能自然处理递归。
3. **终止性**：上下文数量有界（`k` 截断或 `max_call_sites` 封顶）；
   每个 `(函数, 块, 上下文)` 的环境是有限原点集合上的单调并；
   循环回边、递归调用都只会向有限格继续做并运算，故不动点必然到达。
4. **流图与路径**：不动点推进的同时维护一张单调流图，节点包括指令、
   块入口、分支、形参绑定、调用实参绑定、调用返回汇聚、函数出口；
   **每条边都标注它实际传播了哪些污点 origin**（仅控制可达、未传污点的边
   标签为空）。不动点结束后，对每个 `(sink, origin)` 在图上做带环剪枝的
   有界反向 DFS，只走“确实传播了该 origin”的边，得到**从源到汇的正向路径**，
   路径长度/条数可配置（`max_path_len` / `max_paths`）。

### 保守近似与已知误报（重要）

| 近似 | 行为 | 已知后果 |
|---|---|---|
| 分支条件**不解释** | `if/while` 的两条边都视为可能 | 污点只在一条分支被清洗时，合流后仍报——**条件清洗误报** |
| 循环条件不解释 | 0 次迭代与任意次迭代都视为可能 | 污点仅在循环体内清洗时，循环后 sink 仍报（同属条件清洗误报） |
| 无强更新 / may 合流 | 赋值不清空旧污点 | 强更新可排除的路径可能保留（偏向多报） |
| 有限上下文 `k` | 超出深度的栈帧被丢弃并合并调用者 | `k` 越小越容易把不同调用点混淆，产生误报；`k=0` 最明显 |
| 不透明调用 | 无函数体可看，任一实参带污点则假定返回值带污点 | 外部函数若实际清洗了数据，会多报 |
| 入口形参默认受污染 | 保守的攻击者模型 | 可信入口可通过配置关闭 |
| 不建模常量条件/路径可行性 | 不做约束求解 | 不可达路径上的 sink 也可能被报 |

样例 `examples/programs/conditional_sanitize.tl` 与
`context_precision.tl`（用 `--k 0`）分别演示前两类**预期内的已知误报**。

**不声明完备性**：真实语言的别名、过程值、异常、并发、反射、库语义等均不在
本语言/分析器范围内。

## 6. JSON 服务

`POST /analyze`（仅标准库 `http.server`）：

```json
{
  "source": "func main() { sink(source()); }",
  "config": {
    "k": 2,
    "entry_points": ["main"],
    "sources": ["source"],
    "sinks": ["sink"],
    "sanitizers": ["sanitize"],
    "max_paths": 8,
    "max_path_len": 160,
    "max_call_sites": 256
  },
  "include_ir": true,
  "tainted_entry_params": true
}
```

- `200 {"ok": true, "result": {...}}`
- `400 {"ok": false, "error": {type,message,span?}}`：请求合法但程序有错
  （LexError/ParseError/BuildError/ConfigError）。
- `422`：请求体不是合法 JSON 或缺少 `source` 等。
- `GET /health`：健康检查。

`result` 关键字段：

- `findings[]`：每条含 `sink`、`source`（origin 种类/位置）、`paths[]`
  （源→汇的有序步骤，每步带 kind、label、context、源码 span/text）、
  `path_count`、`paths_truncated`；
- `sinks_without_taint[]`：程序中存在但无污点到达的 sink（“安全”点）；
- `opaque_calls[]`：不透明调用及其是否被假定污染；
- `contexts[]` / `truncated_contexts[]`：实际出现的调用上下文及被深度截断者；
- `stats`：函数/块/指令/调用点/上下文数量、不动点处理步数、流图节点/边数；
- `ir`：IR 转储（`include_ir:false` 时省略）。

请求样例与**真实响应**见 `examples/requests/`（`response_*.json` 由实际服务返回）。

## 7. 验收场景一览

| 文件 | 场景 | 预期（k=2, entry=main） |
|---|---|---|
| `basic.tl` | 直接 source→sink | 1 告警 |
| `cross_function.tl` | 多层参数/返回值传播 | 2 个 sink 告警，路径含 param/call-return |
| `sanitized.tl` | 跨函数强清洗 | 清洗后 sink 安全，原始变量 sink 告警 |
| `conditional_sanitize.tl` | 仅一条分支清洗 | **1 个已知误报** |
| `context_precision.tl` | 同函数两处调用（脏/净） | k=2：1 告警；**k=0：2 告警（已知误报）** |
| `recursion.tl` | 直接递归 | 1 告警，分析终止 |
| `loop_and_branch.tl` | 循环内条件引入污点 | 1 告警 |
| `opaque_call.tl` | 外部函数 | 脏实参→1 告警；净实参→安全 |

## 8. 测试

```
python3 -m unittest discover -s tests -v
```

覆盖：词法（token/注释/非法字符/span）、语法（优先级/语句/错误）、
IR 降级（调用终结符、分支/循环结构、UID 全局唯一、arity、span 文本）、
分析器（入口污点、赋值/运算、分支、跨函数调用、清洗器、递归、循环、
不透明调用、上下文深度精度、**已知误报断言**、源→汇路径结构）、
以及真实 socket 上的 HTTP 服务（200/400/422/404、health、IR 省略）。

实际执行命令、输出与未通过项的记录见 [`RUNLOG.md`](./RUNLOG.md)。
