# 正则自动机引擎（regex-automata engine）

一个**纯后端**的正则小子集语言工具链与 JSON 服务，从零实现，不依赖任何
第三方库，核心解析与分析**不使用现成编译器/正则库**（引擎代码中不
`import re`）。

- 手写词法分析（Token 保留源码位置）
- 手写递归下降语法分析（AST 保留源码位置）
- **Thompson 构造 NFA**
- 基于**活跃状态集合**的线性时间模拟匹配（无回溯）
- 独立的 JSON HTTP 服务（标准库 `http.server`）
- 一个**独立算法**的参考解释器，用于穷举差分验证
- 自动化测试（标准库 `unittest`，无需 pip 安装）

---

## 目录结构

```
regex_automata/
  locations.py    源码位置 Span、行列定位、错误片段渲染
  errors.py       LexError / ParseError（带 span）
  tokens.py       Token 定义
  lexer.py        手写词法分析器
  ast.py          AST 节点（全部带 span）
  parser.py       手写递归下降分析器
  predicates.py   码点级字符判定（含 Unicode 策略）
  nfa.py          Thompson NFA 构造
  matcher.py      NFA 集合模拟（search / fullmatch / prefix）
  reference.py    独立参考解释器（仅测试用的"预言机"）
  compiler.py     source -> tokens -> AST -> NFA 门面
  service.py      JSON HTTP 服务
  __main__.py     命令行
tests/            自动化测试（unittest）
examples/         请求样例与 curl 脚本、样例输出
scripts/          测试脚本、与 python re 对比的基准脚本
docs/
  language.md     语言规范（语法 / Unicode / 锚点 / 匹配语义）
  benchmark_results.txt  灾难性回溯对比实测数据
```

---

## 支持的语言（摘要）

连接、选择 `|`、括号、有限重复 `* + ? {m} {m,} {m,n}`；字符类
`[...]`、取反、区间、`\d \w \s` 等；转义 `\n \t \xHH \uHHHH`；
锚点 `^ $ \b \B`；`.`。

明确按 **Unicode 码点**匹配；**不支持反向引用**、捕获组、环视、
非贪婪/占有量词、`\p{}` 等。完整定义见 [`docs/language.md`](docs/language.md)。

匹配方式：`search`（默认，**最左起点、同点最长**）、`fullmatch`、`prefix`。

---

## 快速开始

需要 Python 3.10+（开发于 3.12）。无需安装依赖。

### 命令行

```bash
# 匹配（输出 JSON；退出码 0=命中, 1=未命中, 2=编译错误）
PYTHONPATH=. python3 -m regex_automata match '(ab|a)*b' 'aab' --mode fullmatch
PYTHONPATH=. python3 -m regex_automata match 'x' 'abc'          # exit 1
PYTHONPATH=. python3 -m regex_automata compile 'a{1,2}' --nfa   # 打印 AST/NFA
```

### JSON 服务

```bash
PYTHONPATH=. python3 -m regex_automata serve --host 127.0.0.1 --port 8080
```

| 方法 | 路径 | 请求 |
|---|---|---|
| GET | `/health` | — |
| POST | `/compile` | `{"pattern": "..."}` |
| POST | `/match` | `{"pattern": "...", "text": "...", "mode": "search"\|"fullmatch"\|"prefix"}` |

`mode` 省略时为 `search`。样例：

```bash
curl -s -X POST http://127.0.0.1:8080/match \
  -H 'Content-Type: application/json' \
  -d '{"pattern":"(ab|a)*b","text":"aab","mode":"fullmatch"}'
# {"ok": true, "pattern": "(ab|a)*b", "mode": "fullmatch", "matched": true,
#  "match": {"start": 0, "end": 3, "matched": "aab"}, "nfa_states": 14}
```

错误返回 HTTP 400 并带源码位置：

```json
{"ok": false, "error": {"type": "LexError",
  "message": "quantifier range out of order: {2,1} requires min <= max",
  "line": 1, "column": 2, "span": [1, 6]}}
```

更多请求见 [`examples/curl_samples.sh`](examples/curl_samples.sh)，
实际运行输出见 [`examples/sample_output.txt`](examples/sample_output.txt)。

### 作为库使用

```python
from regex_automata import compile_pattern

p = compile_pattern(r"(ab|a)*b")
p.search("aab")      # Match(start=0, end=3, matched='aab')
p.fullmatch("aab")
p.prefix("aabxyz")
p.to_dict()          # AST 之外，还可导出完整 NFA（含每条边）
```

---

## 为什么不会指数退化

Thompson NFA 匹配维护的是"当前可能处于的**状态集合**"，每读入一个码点
做一次集合转移与 ε-闭包，复杂度 `O(|text| × |NFA|)`，与回溯路径数量无关。
经典灾难性回溯样例实测（完整数据见 `docs/benchmark_results.txt`，
脚本 `scripts/benchmark_backtracking.py`）：

```
pattern          n   thompson(s)    python re(s)
(a+)+b          20      0.00146         0.109
(a+)+b          24      0.00210         1.55
(a+)+b          26      0.00267   > 2.0 (超时被杀)
(a+)+b          30      0.00321   > 2.0 (超时被杀)
(a|a)*b         24      0.00167   > 2.0 (超时被杀)
```

回溯引擎在 n≈26 后超过 2 秒被强制终止，且继续指数增长；Thompson 引擎
始终在数毫秒。该对比中的 `re` 仅用于演示，不是引擎依赖。

---

## 验收方式（实测）

### 1) 限定语法上的穷举差分测试

`tests/test_exhaustive_reference.py` 枚举该语法的全部小模式（989 个）
与小字母表上长度 0–4 的全部字符串（127 个），对 `search / fullmatch /
prefix` 三种模式共 **376,809** 次比较，逐一核对引擎与独立参考解释器
（`reference.py`，记忆化的区间识别关系，算法完全不同）结果一致。

### 2) 灾难性回溯不退化

`tests/test_no_catastrophic.py` 对 `(a?){20}a{20}`、`(a*)*b`、
`(a|aa)*b`、`(a+)+b` 等样例验证绝对耗时上限与随输入增长的非指数特性。

### 运行测试

```bash
bash scripts/run_tests.sh
# 或：
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
```

当前结果：**40 个测试全部通过**（约 9 秒，主要耗时在穷举差分）。

---

## 设计取舍

- **左most-最长**而非 PCRE 的最左-贪婪：因为我们不回溯，直接对 NFA 在每个
  起点取接受的最长结束位置，语义明确、确定。
- 有限重复 `{m,n}` 做 Thompson 展开，设 1000 上限以控制 NFA 体积；无界重复
  用环，不受限。
- `^`/`$` 无多行模式，`$` 不接受结尾换行前的位置（与 Python `re.search` 的
  `$` 不同，规范中显式说明），避免隐藏特例。
- 参考解释器刻意写得朴素（指数级），只用于小输入的差分校验，不对外暴露为
  引擎能力。
