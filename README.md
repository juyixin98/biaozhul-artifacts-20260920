# Mini 差分语法解析（纯后端）

一个用 Python 标准库从零实现的小语言工具链：**手写词法器 + 手写递归下降语法分析器 + 增量（差分）解析 + JSON 服务**。不依赖任何现成编译器 / 解析器生成器 / 第三方包。

- 自定义并文档化的语法（见下文「语言定义」）
- 所有语法节点、诊断都保留源码位置（字符偏移 + 行列号）
- 编辑后**复用未受影响的语法节点**（保持对象身份），受影响部分用同一套解析器重解析
- 模糊测试：随机插入/删除/替换（含字符串内符号、未闭合括号、未闭合字符串、连续编辑），每步与全量解析对比**树结构**与**错误位置**

## 目录结构

```
minilang/
  lexer.py      # 手写词法器：Token / Diagnostic / 行列换算
  parser.py     # 手写递归下降解析器（含错误恢复与节点复用逻辑）
  nodes.py      # 语法节点、子树平移、结构相等比较
  document.py   # Document：编辑应用 + 增量解析驱动
  serialize.py  # 树与诊断 -> JSON
  __main__.py   # CLI：python -m minilang file.ml
server.py       # JSON HTTP 服务（仅标准库）
tests/          # unittest 自动化测试（含 fuzz 差分测试）
examples/       # 示例源码与请求样例
```

## 语言定义（Mini）

### 语法（EBNF）

```ebnf
program   = { decl } ;
decl      = "let" IDENT "=" expr ";"
          | "fn" IDENT "(" [ IDENT { "," IDENT } ] ")" "=" expr ";" ;
expr      = unary { ("+" | "-" | "*" | "/" | "%") unary } ;
unary     = "-" unary | call ;
call      = primary { "(" [ expr { "," expr } ] ")" } ;
primary   = NUMBER | STRING | IDENT | "(" expr ")" ;
```

### 词法规则

- **标识符**：`[A-Za-z_][A-Za-z0-9_]*`；关键字 `let`、`fn`。
- **数字**：`[0-9]+` 或 `[0-9]+ "." [0-9]+`。
- **字符串**：双引号包围，支持 `\n`、`\t`、`\r`、`\"`、`\\` 转义（反斜杠可转义任意下一字符）。字符串内出现的 `+`、`(`、`;`、`//` 等符号**不产生任何语法意义**。字符串遇到换行或文件结束仍未闭合 → 诊断 `unterminated string literal`，字符串在换行前截断。
- **注释**：`//` 到行尾。
- **运算符 / 标点**：`= ; ( ) , + - * / %`。
- 其他字符 → 诊断 `illegal character '<c>'`，跳过该字符继续。

### 优先级与结合性

`*` `/` `%` 高于 `+` `-`；同级左结合；一元 `-` 高于二元运算；调用 `f(...)` 绑定最紧。

### 位置约定

- 所有区间（span）为**半开字符偏移** `[start, end)`，针对源文本的字符下标（Python 字符串下标）。
- JSON 输出同时给出 0 起始的 `start_line/start_col/end_line/end_col`。
- 诊断同样携带半开区间；指向「缺失」位置的诊断（如 `expected ';'`）使用零宽区间 `[p, p)`。

### 语法节点类型

| kind      | 含义             | value 主要字段                                   | children        |
|-----------|------------------|--------------------------------------------------|-----------------|
| `program` | 整个文件         | —                                                | 各声明          |
| `let`     | let 声明         | `name`, `name_span`                              | `[值表达式]`    |
| `fn`      | fn 声明          | `name`, `name_span`, `params[]`, `closed_paren`  | `[函数体]`      |
| `binary`  | 二元运算         | `op`, `op_span`                                  | `[左, 右]`      |
| `unary`   | 一元负号         | `op`, `op_span`                                  | `[操作数]`      |
| `call`    | 调用             | `closed_paren`                                   | `[被调, 参数…]` |
| `paren`   | 括号表达式       | `closed_paren`                                   | `[内部表达式]`  |
| `number`  | 数字字面量       | `text`                                           | —               |
| `string`  | 字符串字面量     | `text`（解码后）, `terminated`                   | —               |
| `ident`   | 标识符引用       | `name`                                           | —               |
| `error`   | 错误恢复节点     | `skipped` 或 `message`                           | —               |

### 错误恢复

解析不抛异常，任何输入都产生完整树 + 诊断列表：

- 缺 `;` / `=` / 标识符 → 诊断后继续（零宽区间定位）。
- 未闭合 `(`（表达式、调用、参数列表）→ 诊断 `unclosed '('`，位置指向开括号，节点标记 `closed_paren: false`。
- 声明之间出现垃圾 token → 跳读到下一个 `let`/`fn`/EOF，产生一个 `error` 节点。

## 增量（差分）解析算法

`Document.apply_edit(start, old_len, new_text)` 把 `text[start:start+old_len]` 替换为 `new_text`，然后：

1. **拼接新文本，全量重新词法分析**（词法廉价，且字符串/注释内的编辑会改变任意远处的 token 划分，重新词法是最稳妥的）。
2. 对上一版语法树的每个**顶层声明节点**，若其区间与编辑区间不相交，则作为「复用候选」：编辑点之前的候选偏移不变，之后的候选整体平移 `delta = len(new_text) - old_len`。
3. 解析器照常自顶向下解析；在每个声明边界处尝试候选，**接受条件**（全部满足才复用）：
   - 候选声明的 token 序列与新词法结果在 kind、文本、字符串闭合标志上逐一相同，且**相对偏移**一致（因此只改空白的编辑也会安全地触发重解析）；
   - 声明之后的**一个前瞻 token** 的 kind/文本一致（错误恢复依赖下一个 `let`/`fn`/EOF 作为停止点；缺 `;` 诊断的位置也指向该 token）；
   - 若声明带有指向前瞻位置的诊断（如 `expected ';'`），前瞻 token 的相对偏移也必须一致。
4. 被接受的节点**保持 Python 对象身份**（`is` 相等），仅将整棵子树的偏移平移 `delta`；该声明携带的解析期诊断一并平移保留。未接受的声明用同一套递归下降解析器重新解析。

因此增量结果与全量解析**逐节点一致**——这是测试直接验证的性质，而不是假设。

> 注意：复用节点的平移是原地修改，旧版结果对象中的节点偏移会被更新为新值。

## 运行

无第三方依赖，Python ≥ 3.10 即可。

```bash
# 运行全部自动化测试
python3 -m unittest discover -s tests

# CLI：解析文件并输出 JSON 树（有诊断时退出码为 1）
python3 -m minilang examples/sample.ml

# 启动 JSON 服务
python3 server.py --port 8000
```

## JSON API

| 方法 | 路径 | 请求体 | 说明 |
|------|------|--------|------|
| POST | `/parse` | `{"text": "..."}` | 一次性全量解析 |
| POST | `/documents` | `{"text": "..."}` | 创建文档，返回 `id` 与解析结果 |
| GET  | `/documents/<id>` | — | 当前文本、树、诊断 |
| POST | `/documents/<id>/edit` | `{"start": n, "delete": m, "insert": "..."}` | 应用编辑并增量解析 |

编辑响应中的 `stats` 字段报告本次复用 / 重解析的声明数，例如 `{"reused": 3, "parsed": 1}`。

请求样例见 [examples/requests.md](examples/requests.md)。

## 验证记录（实际运行，未粉饰）

以下命令在本机（Python 3.12.3，Linux）实际执行：

### 1. 自动化测试

```
$ python3 -m unittest discover -s tests
.........................................
----------------------------------------------------------------------
Ran 41 tests in 0.711s

OK

[fuzz] 720 edit comparisons; reused 1774/2612 declarations (67.9%); steps with diagnostics: 716
```

41 个测试全部通过。其中 fuzz 差分测试覆盖 24 个随机源（一半故意包含未闭合括号/字符串）× 每个 30 步随机编辑，共 **720 次**「增量 vs 全量」的树结构 + 诊断位置对比，全部一致；716 步处于含错误诊断的状态。

### 2. 开发过程中被发现并修复的问题（如实记录）

测试并非一次通过，首轮 `Ran 41 tests ... FAILED (failures=8, errors=1)`：

1. **复用节点丢失诊断（真 bug，fuzz 发现）**：带解析期诊断的声明（如缺 `;`、垃圾恢复节点）被复用时，其诊断没有带入新结果。修复：`ParseResult` 记录每个声明对应的诊断切片，复用时平移保留。
2. **前瞻依赖导致错误复用（真 bug，fuzz 发现）**：错误恢复节点 / 缺 `;` 声明的解析结果依赖于**其后一个 token**（恢复停止点、诊断位置），只比较声明自身 token 切片会在「删除作为恢复边界的 `fn` 关键字」等编辑下产生与全量解析不同的树。修复：复用条件增加前瞻 token 比较（见上文算法第 3 条）。
3. **优先级解析错误（自查发现）**：初版 `parse_expr` 右操作数只解析一元表达式，`1 + 2 * 3` 被错误解析为 `(1+2)*3`。修复：改为 Pratt 风格按优先级递归。
4. **测试断言写错 5 处（测试自身 bug）**：左结合断言方向写反、未闭合括号节点 end 偏移预期值写错、`(` 位置数错、非法字符消息断言写错、两处复用计数预期未考虑「编辑点恰在声明边界 / 编辑落在声明内部」的情形。均修正断言使其符合已文档化的语义。
5. **fuzz 生成器边界 bug**：编辑点位于文本末尾时删除长度算出 0 导致 `randrange` 抛错，已修。

修复后另跑了一次更强的临时压力验证（非交付测试，120 个源 × 60 步编辑，含 50% 单字符键入模拟）：

```
STRESS OK: 7200 comparisons over 120x60 edits in 1.5s
reused 17604/25765 declarations (68.3%), error steps 7175
```

7200 次对比全部一致。

### 3. CLI 实测

```
$ python3 -m minilang examples/sample.ml   # 输出完整 JSON 树，diagnostics 为空
exit=0

$ echo 'let x = (1 + 2
let s = "oops' | python3 -m minilang
# diagnostics: unterminated string literal @[23,28)、unclosed '(' @[8,9)、expected ';' ×2
exit=1
```

### 4. 服务实测（curl，完整样例见 examples/requests.md）

- `POST /parse` 一次性解析：返回树与空诊断，`stats: {"reused": 0, "parsed": 1}`。
- `POST /documents` 创建 4 声明文档，返回 `id`。
- 编辑 1（改 `1` 为 `10`）：`stats: {"reused": 3, "parsed": 1}`，后续声明 span 正确平移。
- 编辑 2（删除 `fn f(x)` 的 `(`）：返回 `expected '('` 等 5 条诊断，位置正确；`{"reused": 3, "parsed": 2}`（fn 声明重解析 + 产生一个 error 节点）。
- 编辑 3（把 `(` 加回）：诊断清空，`{"reused": 3, "parsed": 1}`。
- 越界编辑（`start: 999`）：HTTP 400。

### 已知限制

- 复用粒度为**顶层声明**；声明内部的表达式子树不做独立复用（声明内任何编辑都会整体重解析该声明）。
- 词法为全量重扫（对本语言规模足够快；换取字符串/注释编辑下的正确性）。
- 复用节点的偏移平移是原地修改，持有旧版树对象的调用方会看到更新后的偏移。
