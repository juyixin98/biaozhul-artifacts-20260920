# 差分语法解析（dlang）

一个**纯后端**的小语言工具链：自研词法分析 + 递归下降语法分析 + **增量解析**，
以及一个基于 Python 标准库的 JSON HTTP 服务。核心解析与分析全部手写，
不依赖任何编译器框架或第三方解析库。

增量解析在文本编辑后**复用未受影响的语法子树**（保留节点身份），
并保证复用节点的源码范围正确平移。验收方式是**差分测试**：对同一文本，
增量解析的语法树和错误位置必须与从头全量解析**逐字段一致**。

- 语言：Python 3.10+（仅标准库，无第三方依赖）
- 无前端、无 UI、无数据库；服务状态保存在内存中

---

## 目录结构

```
dl/
  nodes.py        语法树节点 Node、诊断 ParseError
  lexer.py        词法分析（字符串/注释/运算符，词法错误不中断）
  parser.py       递归下降 + Pratt 表达式；全量与增量共用同一套例程
  incremental.py  Edit / Document：令牌映射 + 带复用上下文的重解析
  diff.py         规范化树、全量↔增量一致性断言、JSON 序列化
  server.py       http.server 实现的 JSON 服务
  cli.py          命令行（parse / lex / 单次编辑演示）
tests/            53 个自动化测试（含随机差分测试与 HTTP 端到端测试）
examples/         合法示例 sample.dl、错误恢复示例 errors.dl
examples/requests/  HTTP 请求样例与预期响应
```

---

## 快速开始

```bash
# 全量解析（输出 JSON，有错误时退出码为 1）
python3 -m dl.cli parse examples/sample.dl | less

# 只做词法分析
python3 -m dl.cli lex examples/sample.dl

# 单次编辑的增量解析演示（在开头插入一条声明），输出含复用统计
python3 -m dl.cli edit examples/sample.dl --start 0 --end 0 \
    --text $'var v = 0;\n'

# 启动 JSON 服务（默认 127.0.0.1:8000）
python3 -m dl.server --port 8000

# 运行全部测试
python3 -m unittest discover -s tests -v
```

---

## 语言定义（dlang）

这是一个用于演示增量解析的**表达式 + 声明**小语言，不是完整编程语言
（没有类型系统、没有求值/代码生成）。

### 词法

| 类别 | 内容 |
|---|---|
| 空白 | 空格、制表符、换行（被忽略） |
| 注释 | `//` 到行尾 |
| 标识符 | 字母或 `_` 开头，后跟字母/数字/`_` |
| 关键字 | `var fn return if else while break continue true false null` |
| 数字 | 一个或多个十进制数字（整数） |
| 字符串 | `"…"` 或 `'…'`，支持 `\" \' \\ \n \t` 转义；**不跨行** |
| 运算符 | `+ - * / % < > <= >= == != = && || ! ( ) { } [ ] ( ) , ;` |

**字符串内符号**：字符串内部的括号、分号、运算符等一律**不产生令牌**，
整个字面量是一个 `string` 令牌。例如 `"(1 + 2);"` 内部的 `(`、`+`、`)`、
`;` 都不会影响外层语法结构。

### 语法（EBNF 风格）

```
program     := statement*
statement   := varDecl | fnDecl | block | ifStmt | whileStmt
             | returnStmt | breakStmt | continueStmt | exprStmt | ';'
varDecl     := 'var' IDENT ('=' expr)? ';'
fnDecl      := 'fn' IDENT '(' params? ')' block
params      := IDENT (',' IDENT)*
block       := '{' statement* '}'
ifStmt      := 'if' '(' expr ')' block ('else' (block | ifStmt))?
whileStmt   := 'while' '(' expr ')' block
returnStmt  := 'return' expr? ';'
breakStmt   := ('break' | 'continue') ';'
exprStmt    := expr ';'

expr        := assign
assign      := logical ('=' assign)?                 # = 右结合，优先级最低
logical     := comparison (('&&' | '||') comparison)*
comparison  := additive (('==' | '!=' | '<' | '>' | '<=' | '>=') additive)*
additive    := term (('+' | '-') term)*
term        := unary (('*' | '/' | '%') unary)*
unary       := ('-' | '!') unary | postfix
postfix     := primary ('(' args? ')' | '[' expr ']')*
primary     := NUMBER | STRING | 'true' | 'false' | 'null' | IDENT
             | '(' expr ')' | '[' args? ']'
args        := expr (',' expr)*
```

下标 `a[i]` 由词法的 `[ ]` 与 postfix 解析支持。

### 节点格式

每个节点带：

- `kind`：节点类型（`Program / VarDecl / FnDecl / Block / If / Else /
  While / Return / Break / Continue / ExprStmt / Empty / Junk /
  Assign / Binary / Unary / Call / Group / Array / ParamList / ArgList /
  Number / String / Ident / Literal / ErrorExpr`）
- `start`、`end`：源码字符位置，**半开区间 `[start, end)`**（与 Python
  切片一致，`source[start:end]` 即节点文本）
- `tok_start`、`tok_end`：令牌流半开区间（增量复用据此重定位）
- `text`：叶子负载（数字原文、字符串值、标识符名、运算符等）
- `children`：固定顺序的子节点
- `node_id`：解析期单调编号；**被复用的节点保留旧 ID**，新节点取更大 ID

### 错误恢复（确定性）

解析器遇到错误不抛异常、不停止，而是产生 `ParseError{message,start,end}`
并按确定性策略恢复，使得**同样的文本永远得到同样的树和错误**：

- 结构性错误（缺 `(` `)` `{`、if/while/函数头损坏、var 缺名字等）→
  按括号深度同步到下一个 `;`，生成 `Junk` 节点继续；
- 缺分号 → 报错但**不消费**当前令牌（dangling 边界错误），节点仍完整；
- 运算符后缺右值 → 报错并保留已解析的左值；
- 未闭合括号 / 数组 / 块 → 错误锚在下一个令牌或 EOF，继续向后解析；
- 未闭合字符串、非法字符 → 词法阶段产生**错误令牌**，错误位置精确。

---

## 增量解析是怎么工作的

`dl.incremental.Document` 保存当前文本与上一次解析结果（树、令牌流、
每个解析例程的入口表）。一次编辑表示为
`Edit(start, end, new_text)`（插入时 `start==end`，删除时 `new_text==""`）。

编辑后：

1. **重新词法分析整个文档**（线性、便宜，且天然正确处理“编辑点落在
   字符串/注释内部”）；
2. 构造**旧令牌 → 新令牌**的部分双射：替换区间左侧按下标直映，
   右侧从 EOF 反向按 `(类型, 原文)` 后缀对齐，相交令牌失效；
3. 用带**复用上下文**的同一个 `Parser` 重解析。每个解析例程入口先在旧
   入口表中查找同入口节点，满足以下**全部**条件才整棵复用（克隆并跳过
   其令牌区间），否则照常解析：
   - 节点消费的每个旧令牌都映射为**连续**的新令牌；
   - 新旧令牌逐个相同（类型 + 原文）——编辑过的字符串内部符号必然失配；
   - 节点内没有词法错误令牌（未闭合字符串可能被后续编辑补全）；
   - 节点（含后代）**不携带任何语法错误**（错误恢复结构依赖编辑点上下文，
     含错节点本就“受影响”；错误由重解析区域重新产生，位置天然正确）；
   - 表达式尾后没有新插入的续接令牌（postfix `(` 或更高/同级中缀运算符）；
   - 块的闭合 `}` 仍紧接块体末尾（否则块尾插入了新语句）。

复用节点的字符范围由其边界令牌在新文本中的位置决定，所以**节点范围
平移由构造保证正确**，而不是手工对节点加减偏移量。

`Program` 始终整体重解析（其子声明各自复用），因为它逻辑上覆盖到 EOF。

### 为什么差分测试能当正确性标尺

全量解析与增量解析走的是**同一套解析例程**；增量只是多了“入口处能否
复用一棵干净旧子树”的判定。因此对任意文本，只要复用判据没有误判，
增量结果就应当与对该文本从头全量解析的结果完全相同。差分测试直接
逐字段比较这两者，能捕获令牌映射、复用判定、范围平移中的任何偏差。

---

## HTTP JSON 服务

启动：`python3 -m dl.server --host 127.0.0.1 --port 8000`
请求/响应均为 UTF-8 JSON；所有字符位置均为半开区间 `[start, end)`。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/parse` | 一次性全量解析 `{"source": "..."}` |
| POST | `/documents` | 创建增量文档 `{"source": "..."}` |
| GET | `/documents` | 列出文档（id / version / length） |
| GET | `/documents/{id}` | 查看当前文本与解析结果 |
| POST | `/documents/{id}/edits` | 应用一次或顺序多次编辑（增量解析） |
| DELETE | `/documents/{id}` | 删除文档 |

`POST /documents/{id}/edits` 的请求体：

```json
{ "edits": [ {"start": 0, "end": 0, "text": "var z = 0;\n"} ] }
```

单个编辑也可简写为 `{"start": 3, "end": 5, "text": "..."}`。
响应在解析结果之外额外带 `version`（已应用编辑次数）和
`reuse_events`（本次解析复用的子树个数，用于观察增量效果）。

`examples/requests/` 下有可直接运行的 `curl` 样例（`.http` 与 `.sh`）。

响应（节选）：

```json
{
  "tree": {
    "kind": "Program", "start": 0, "end": 11,
    "tok_start": 0, "tok_end": 5, "text": null,
    "children": [
      { "kind": "VarDecl", "start": 0, "end": 10, "...": "...",
        "children": [
          { "kind": "Ident", "text": "x", "start": 4, "end": 5 },
          { "kind": "Binary", "text": "+", "...": "...",
            "children": [ {"kind":"Number","text":"1"},
                          {"kind":"Number","text":"2"} ] }
        ]}
    ]},
  "errors": []
}
```

`POST /parse` 支持 `"include_tokens": true` 附带令牌流。

---

## 测试与验收

```bash
python3 -m unittest discover -s tests -v
```

- `test_lexer.py`：字符串内符号不切词、未闭合字符串、注释、非法字符；
- `test_parser.py`：优先级/结合性、声明结构、各类错误与错误**位置**；
- `test_incremental.py`：复用保留 node_id、范围平移、连续编辑、
  编辑点在字符串内、未闭合括号补全/再删除等；
- `test_differential.py`（**验收核心**）：随机插入/删除后，每一步都用
  `dl.diff.assert_same` 比较增量结果与全量解析的
  **规范化树 + 错误（消息、start、end）**；
- `test_server.py`：在真实 HTTP 端口上做端到端请求测试。

随机差分覆盖：字符串内插入括号/引号、未闭合括号风暴、大量连续编辑、
随机拼接的合法/非法种子程序。测试固定随机种子，可复现。

### 复用确实发生

差分测试不仅校验一致性，还断言随机游走中有大量步骤真正复用了节点；
CLI 的 `edit` 子命令和服务响应的 `reuse_events` 字段也会报告复用子树数。

---

## 已知边界（设计取舍）

- 这是解析器演示语言：不做名称解析、类型检查、求值或代码生成。
- 词法每次全量重扫（线性开销）；增量收益主要体现在语法树复用与
  节点身份保持，而非跳过词法。
- 含错误的节点不参与复用，换取错误位置在任意编辑序列下都与全量
  解析严格一致；错误文档中未受影响的干净声明仍会复用。
