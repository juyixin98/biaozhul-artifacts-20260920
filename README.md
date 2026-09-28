# 正则词元规则词法器后端（Lexer Generator & Overlap Diagnostics）

从正则词元规则生成词法器，并对规则集做**两两重叠最短见证**与**不可达规则**诊断。
技术栈：Python 3.12 · FastAPI · SQLite。全部数据使用本地合成夹具，无外部账号依赖。

## 1. 工程边界

模块按职责拆分为四个边界，边界之间只传递显式数据结构或带类别的错误：

```
app/
├── corpus/      语料规范：词元规则 schema（RuleSpec/RuleSetSpec）；
│                Unicode 范围与 LF 换行模式在此固定
├── kernel/      挖掘内核：
│                  regex_parser.py  递归下降解析 -> 不可变正则 AST
│                  nfa.py           Thompson NFA（码点区间转移）
│                  dfa.py           区间划分的子集构造（确定化）
│                  diagnostics.py   重叠最短见证 + 不可达规则（一次 BFS）
│                  compiler.py      解析→空串拒绝→NFA→DFA→诊断 编排
│                  lexer.py         最长匹配 + 显式优先级 的词法扫描
├── index/       索引与模型：SQLite 持久化规则集、诊断结果、运行日志
├── query/       查询验证：请求体 schema 校验与资源配额（边界输入校验）
├── errors.py    四类错误契约（见 §4）
├── config.py    配置与资源上限（环境变量可覆盖）
├── runlog.py    结构化运行日志（run_id 可重放）
└── main.py      FastAPI 装配（create_app 工厂）
```

## 2. 词法选择规则（固定语义）

1. **最长匹配优先**：取剩余输入上任意规则接受的最长前缀作为词元。
2. **其次显式规则优先级**：同样长度时，`(priority, 声明下标)` 最小者胜；
   `priority` 缺省取声明下标，因此并列由声明顺序唯一决定。
3. **可匹配空串的普通词元规则一律拒绝**（编译期 `INPUT_ERROR`），
   因此每个词元至少消费一个字符，扫描必然终止。

## 3. 固定的字符语义（corpus 级常量，不可按规则集配置）

- 字母表：Unicode 码点 `U+0000..U+10FFFF`。
- 换行模式：**LF**——仅 `U+000A` 是换行；`U+000D` 是普通字符。
- `.` 匹配除 `U+000A` 外的任意码点。
- `\d=[0-9]`、`\w=[0-9A-Za-z_]`、`\s=[\t-\r, 空格]`（ASCII 语义，
  与独立 oracle 使用的 Python `re.ASCII` 一致）。
- 取反类 `[^...]`、`\D \W \S` 在整个字母表上取补，**因此可以匹配换行**。
- 支持：字面量、分组、`|`、`* + ?`、`{m}` `{m,}` `{m,n}`、字符类与区间、
  上述转义。

## 4. 错误契约（四类，可在响应与运行日志中区分）

| 类别 | HTTP | 触发场景 |
|---|---|---|
| `INPUT_ERROR` | 400/404 | 正则语法错、空串规则、非法区间、请求体缺字段、未知规则集/运行 |
| `STATE_CONFLICT` | 409 | 规则集名称已存在 |
| `RESOURCE_EXHAUSTED` | 413 | 规则数/模式长度/重复次数/NFA·DFA 状态数/输入长度超限 |
| `COMPUTATION_FAILURE` | 500 | 未预期的内部失败 |

错误响应统一形如：

```json
{"error": {"category": "INPUT_ERROR", "message": "...", "detail": {...}},
 "run_id": "run_...."}
```

词法扫描遇到无规则可匹配的字符不属于请求级错误：HTTP 200，响应里
`error` 给出失败类别、字符与原偏移，`tokens` 仍含此前成功的前缀。

## 5. API

| 方法与路径 | 说明 |
|---|---|
| `POST /api/rulesets` | 校验→编译→诊断→入库，返回 `ruleset_id` 与诊断 |
| `GET  /api/rulesets/{id}` | 取回存储的规格与诊断 |
| `POST /api/rulesets/{id}/lex` | 词法分析；词元带 `start/end` 原偏移 |
| `GET  /api/runs/{run_id}` | 按运行编号重放结构化日志 |
| `GET  /api/health` | 存活检查 |

规则集请求体：

```json
{
  "name": "demo",
  "rules": [
    {"name": "KW",  "pattern": "if|else",        "priority": 0},
    {"name": "ID",  "pattern": "[a-z][a-z0-9]*", "priority": 1},
    {"name": "WS",  "pattern": "[ \\t\\n]+",     "priority": 2}
  ]
}
```

词法响应词元：`{"rule", "text", "start", "end"}`（半开区间，Python 字符串偏移）。

## 6. 配置（环境变量，括号内为默认值）

| 变量 | 默认 | 含义 |
|---|---|---|
| `LEXER_DB_PATH` | `data/lexer.db` | SQLite 路径 |
| `LEXER_MAX_RULES` | 128 | 每规则集规则数上限 |
| `LEXER_MAX_PATTERN_CHARS` | 2000 | 单条模式长度上限 |
| `LEXER_MAX_REPEAT` | 1000 | `{m,n}` 计数上限 |
| `LEXER_MAX_NFA_STATES` | 4096 | NFA 状态上限 |
| `LEXER_MAX_DFA_STATES` | 8192 | 确定化状态上限 |
| `LEXER_MAX_INPUT_CHARS` | 1000000 | 单次词法输入上限 |

## 7. 从干净目录复现

需要 Python 3.12（依赖版本固定在 `requirements.txt`）：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# 启动服务
uvicorn app.main:app --port 8000

# 另一个终端：创建规则集
curl -s -X POST localhost:8000/api/rulesets \
  -H 'Content-Type: application/json' \
  -d '{"name":"demo","rules":[
       {"name":"KW","pattern":"if|else","priority":0},
       {"name":"ID","pattern":"[a-z]+","priority":1},
       {"name":"WS","pattern":"[ \\t\\n]+","priority":2}]}'

# 词法分析（把 rs_xxx 换成上一步返回的 ruleset_id）
curl -s -X POST localhost:8000/api/rulesets/rs_xxx/lex \
  -H 'Content-Type: application/json' -d '{"text":"if ifx else"}'

# 用 run_id 重放某次编译/请求的中间状态
curl -s localhost:8000/api/runs/run_xxxx
```

不起服务也可跑进程内端到端演示：

```bash
python3 scripts/smoke_demo.py
```

## 8. 测试与独立核验

```bash
pytest                      # 全部测试
pytest --cov=app --cov-report=term-missing
pytest -k overlap           # 只跑重叠/不可达相关
```

测试覆盖任务要求的四个过程，且断言**具体结果与失败类别**：

- `tests/test_keywords_identifiers.py` — 关键字与标识符（最长匹配压过优先级）、原偏移。
- `tests/test_numbers.py` — 整数/浮点/十六进制数字格式与词法失败位置。
- `tests/test_comments.py` — 行注释吞并关键字直到（不含）换行；偏移拼回原文。
- `tests/test_priority_ties.py` — 同长匹配由显式优先级、再由声明顺序裁决。
- `tests/test_overlap_witness.py` — 重叠最短见证，并用**独立 automata 交集核验**。
- `tests/test_fixed_semantics.py` — Unicode 范围、LF 换行、取反类补集。
- `tests/test_validation.py` — 四类错误（空串拒绝、语法错、状态冲突、资源耗尽）。
- `tests/test_run_logs.py` — run_id 重放、关键中间状态、错误类别可区分。
- `tests/test_kernel_direct.py` — 直接对内核做 Python `re` 采样交叉验证。

**参考答案不来自被测核心自身**：`tests/oracle.py` 独立于内核（不 import 任何
`app.kernel` 代码），用 Python 标准库 `re`（`re.ASCII`）做成员判定，并在模式
字符构成的字母表上**穷举更短串**核验见证的最短性与字典序唯一性；测试再把
内核给出的见证与穷举得到的规范见证逐字断言相等。

## 9. 夹具

`fixtures/rulesets/*.json` 为本地合成规则集（关键字/数字/注释/同长/
重叠不可达/空串非法/资源膨胀），可直接作为 `POST /api/rulesets` 的请求体。

实际执行结果见 [docs/VERIFICATION.md](docs/VERIFICATION.md)。
