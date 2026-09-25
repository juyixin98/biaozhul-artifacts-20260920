# renfa — 正则自动机引擎

一个**纯后端、零第三方依赖**的正则小子集工具链：手写词法/语法分析 → AST（保留
全部源码位置）→ **Thompson 构造 NFA** → 集合驱动的**线性时间、无回溯**匹配模拟，
并附带一个标准库实现的 **JSON HTTP 服务**。另含一个朴素回溯参考解释器，仅用于
测试对照与"灾难性回溯"演示。

> 核心解析与分析（词法、语法、NFA 构造、模拟）全部手写，**没有**使用任何现成
> 正则库或编译器框架；Python 标准库 `re` 仅在测试中作为交叉验证的参考。

## 目录

- [支持的正则子集](#支持的正则子集)
- [Unicode（码点）语义](#unicode码点语义)
- [锚点语义](#锚点语义)
- [匹配与搜索语义](#匹配与搜索语义)
- [明确不支持的特性](#明确不支持的特性)
- [快速开始](#快速开始)
- [JSON 服务 API](#json-服务-api)
- [项目结构](#项目结构)
- [工作原理（工具链）](#工作原理工具链)
- [为什么不会灾难性回溯](#为什么不会灾难性回溯)
- [测试与验收](#测试与验收)

---

## 支持的正则子集

| 构造 | 写法 | 示例 | 说明 |
|---|---|---|---|
| 字面量 | 任意普通码点 | `a`、`中`、`😀` | 按码点原样匹配 |
| 连接 | 相邻书写 | `abc` | |
| 选择 | `|` | `ab\|cd` | 优先级最低 |
| 括号 | `(...)` | `(ab\|c)d` | 仅分组、定优先级；**不捕获** |
| 重复（≥0） | `*` | `a*` | 0 次或多次 |
| 重复（≥1） | `+` | `a+` | 1 次或多次 |
| 可选 | `?` | `ab?` | 0 次或 1 次 |
| 精确 n 次 | `{n}` | `a{3}` | |
| 至少 n 次 | `{n,}` | `a{2,}` | |
| n 到 m 次 | `{n,m}` | `a{1,4}` | `0 ≤ n ≤ m` |
| 任意码点 | `.` | `a.c` | 不匹配 `\n` |
| 起始锚点 | `^` | `^abc` | 零宽，见下 |
| 结束锚点 | `$` | `abc$` | 零宽，见下 |
| 转义 | `\` | `\.`、`\\`、`\(` | 标点转义为字面量 |
| 控制字符转义 | | `\n \t \r \f \v \0` | |
| Unicode 转义 | | `中`、`\u{1F600}` | 拒绝代理码点 D800–DFFF |

形式文法（EBNF）：

```ebnf
alternation := concat ('|' concat)*
concat      := quantified*
quantified  := atom quantifier?
quantifier  := '*' | '+' | '?' | '{' n '}' | '{' n ',' '}' | '{' n ',' m '}'
atom        := literal | '.' | '^' | '$' | '(' alternation ')'
```

规则与限制：

- 空分支合法：`a|`、`|a`、`()` 都能匹配空串。
- 量词不能直接叠加：`a**`、`a?{2}` 报语法错；需要时显式加括号 `(a*)+`。
- `{` 只用于量词，非法花括号直接报错；要匹配字面花括号写 `\{`、`\}`。
- 有界重复通过复制 NFA 片段实现，NFA 状态数与重复次数成**线性**关系，
  上限 `MAX_STATES = 20000`（如 `a{99999}` 会得到编译期错误而非撑爆内存）。

## Unicode（码点）语义

- 一切位置、长度、偏移的单位都是 **Unicode 码点（code point）**，与 Python
  `str` 索引一致；**不是字节**，也不是 UTF-16 代码单元。`😀`（U+1F600）算
  **1 个字符**，`😀{2}` 匹配两个 emoji。
- 匹配是逐码点的**精确相等**：不做大小写折叠，也不做 Unicode 规范化
  （`é` U+00E9 与 `e`+U+0301 不相等；后者是两个码点，`..` 才能匹配）。
- `.` 匹配除 `\n`（U+000A）以外的任意**单个码点**。
- 源码位置同样是码点偏移，并额外给出从 1 开始的行/列号（换行只认 `\n`）。

## 锚点语义

- `^`：零宽条件，**仅在整个文本的码点位置 0** 可通过。
- `$`：零宽条件，**仅在码点位置 len(text)**（全文最后）可通过。
- **没有** MULTILINE：`^`/`$` 不会匹配内部换行之后/之前的位置。
- `$` **不**匹配末尾换行符之前（不同于 Python `re$` 的默认行为）。
  若要表达"以换行结尾前的 abc"，请显式写 `abc\n$`。
- **没有** `\b` 词边界（词法期即报错）。

## 匹配与搜索语义

- `fullmatch`：整个文本必须完全匹配。
- `match`：锚定起始位置 0，返回该处的最长匹配。
- `search`：**左起始优先；同一起始位置取最长结束**（POSIX 风格 longest
  match）。歧义情况下与回溯型引擎按贪婪顺序报告的结果可能不同——本引擎明确
  采用左起始+最长。例：`a|ab` 在 `ab` 上匹配 `ab`（不是 `a`）。
- `findall`：非重叠、左起始优先、同起始最长；零宽匹配之后前进一个码点。
- 空模式 `""` 在每个位置都匹配空串。

## 明确不支持的特性

字符类 `[abc]`、简写类 `\d \w \s`（及大写）、词边界 `\b \B`、反向引用、
捕获组/命名组、非贪婪量词 `*?`、环视/断言 `(?=)`、行内 flag `(?i)`、
MULTILINE、`\xHH` 等。遇到这些构造会在词法/语法期报错并给出源码位置。
本项目范围也**不包含反向引用**——NFA 表达的是正则语言，反向引用会使其成为
非正则语言，无法由有限自动机识别。

## 快速开始

需要 Python 3.10+（开发于 3.12；仅用标准库）。

```bash
# 命令行直接匹配
python -m renfa.cli match -p 'a(b|c)*' -t abbc --op search

# 启动 JSON 服务
python -m renfa.service --host 127.0.0.1 --port 8080
#   或
python -m renfa.cli serve --port 8080
```

作为库：

```python
from renfa import Regex

r = Regex("(ab|c)(d|e)*f{1,2}")
m = r.fullmatch("cddeff")          # -> Match
m.span          # (0, 6)，码点偏移
m.text          # 'cddeff'

Regex("a{1,2}").findall("aaa")     # -> [Match(0,2,'aa'), Match(2,3,'a')]
Regex("中+").search("x中中y")       # -> Match(1,3,'中中')
```

错误带源码位置：

```
$ python -m renfa.cli match -p '*abc' -t abc
正则语法错误: '*' 前面没有可重复的原子 (第 1 行, 第 1 列)
  |
  | *abc
  | ^
```

## JSON 服务 API

`GET /healthz` → `{"ok": true, "engine": "renfa", "version": ...}`

`POST /match`，请求体：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `pattern` | string | 是 | 正则子集模式 |
| `text` | string | 否 | 待匹配文本，默认 `""` |
| `op` | string | 否 | `fullmatch`/`match`/`search`/`findall`，默认 `search` |
| `debug` | bool | 否 | 为 true 时附带 tokens/AST/NFA |

成功（200）：

```json
{
  "ok": true, "op": "fullmatch", "pattern": "(ab|c)(d|e)*f{1,2}",
  "matched": true,
  "match": {"start": 0, "end": 6, "text": "cddeff"},
  "matches": []
}
```

错误（400）：

```json
{"ok": false, "error": {"type": "syntax",
  "message": "括号 '(' 没有对应的 ')'", "location": "1:2"}}
```

`type` 取值 `syntax`（词法/语法）、`compile`（NFA 超状态上限）、
`request`（请求格式）。请求体上限 1 MiB。

请求/响应样例见 [`examples/`](examples/)，可用
`bash examples/curl_examples.sh` 一键演示。

## 项目结构

```
renfa/
  source.py    源码、Span（码点偏移 + 行列号）
  errors.py    RegexSyntaxError / RegexCompileError
  lexer.py     手写词法分析器
  ast_nodes.py AST 节点（每个节点带 span）
  parser.py    手写递归下降语法分析
  nfa.py       Thompson 构造 NFA（含 {n,m} 片段复制、状态上限）
  engine.py    NFA 模拟（fullmatch/match/search/findall），无回溯
  reference.py 朴素回溯参考解释器（仅测试/演示，带步数预算）
  service.py   标准库 http.server 的 JSON 服务
  cli.py       命令行入口
tests/         unittest 自动化测试（5 个文件）
scripts/       bench_backtrack.py 灾难性回归基准
examples/      请求/响应样例、curl 脚本、基准结果
```

## 工作原理（工具链）

1. **词法** `lexer.py`：逐码点扫描，产出带 `Span` 的 token；处理量词、转义、
   Unicode 转义；显式拒绝字符类等超集特性。
2. **语法** `parser.py`：递归下降（alternation → concat → quantified → atom），
   `*`/`+`/`?` 归一化为统一的 `Repeat(mn,mx)` AST 节点。
3. **Thompson 构造** `nfa.py`：每个语法结构组合成"入口状态—出口（接受）状态"
   片段。选择用分叉+汇合 ε 边，连接串联，`*`/`+`/`{n,}` 用回环边，
   `{n}`/`{n,m}` 复制独立片段。锚点编译为**条件 ε 边**。
4. **NFA 模拟** `engine.py`：维护"当前可能处于的状态集合"，对文本逐码点做
   一步确定的转移 + ε 闭包。`search` 用带起始标签（tag）的模拟，在一次线性
   扫描中同时跟踪所有可能的起点，按 (最小起点, 最大结束) 选答案。

## 为什么不会灾难性回溯

NFA 模拟**不做任何回溯**：每个码点只被消费一次，第 `i` 个位置的状态集合大小
以 NFA 状态数为上界。匹配耗时为 `O(文本码点数 × NFA状态数 × 出度)`，对固定
模式相对文本长度是**线性**的。

经典回溯杀手 `(a+)+b` 在全 `a` 输入上有指数多种切分方式；实测
（见 [`run_results.md`](run_results.md) 与 `examples/bench_result.json`）：

| n（a 的个数） | 回溯参考解释器步数 | Thompson 边检查数 | Thompson 耗时 |
|---:|---:|---:|---:|
| 8  | 1,800 | 115 | 0.00005 s |
| 16 | 458,768 | 251 | 0.00009 s |
| 20 | 7,340,052 | — | — |
| 1,000 | — | 16,979 | 0.005 s |
| 100,000 | — | 1,699,979 | 0.56 s |

n 每翻倍，Thompson 的边检查数比值约 **2.0**（线性），而回溯步数约 **4 倍**
（二次指数 / 2^(n+1)）。n=20 时回溯已需 734 万步，n=24 起在 200 万步预算内
无法完成；同模式 10 万码点 Thompson 在 1 秒内给出答案。

## 测试与验收

```bash
python -m unittest discover -s tests -v
python scripts/bench_backtrack.py
```

- 解析/位置/错误、引擎语义、Unicode、锚点、左起始最长：单元测试。
- **穷举等价**：对 552 个由受限语法生成的模式 × 字母表 `{a,b}` 全部 31 个
  长度 0..4 字符串（共约 3.4 万对），比较 Thompson 引擎与回溯参考解释器的
  `fullmatch` 布尔结果与 `search` 跨度；无锚点子集再与 Python `re.fullmatch`
  交叉验证。全部一致。
- **灾难性回归**：断言参考解释器步数指数增长并撞预算，同时断言 Thompson 的
  边检查数在 n 翻倍时比值 ≤ 2.6（线性）、大输入毫秒~亚秒级完成。
- **服务**：直接测请求处理函数 + 起真实 HTTP 服务走网络往返。

实际运行命令与结果（含中途发现并修复的问题）如实记录在
[`run_results.md`](run_results.md)。
