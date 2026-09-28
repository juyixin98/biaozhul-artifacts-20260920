# lexgen — 正则词元规则词法器生成与重叠诊断后端

从正则词元规则生成词法器，并给出规则重叠诊断（最短见证）与不可达规则报告。
技术栈：Python 3.12 + FastAPI + SQLite（标准库 `sqlite3`）。全部数据为本地合成夹具，无外部账号与真实业务数据。

## 固定语言契约

- **字母表**：Unicode 码点 U+0000–U+10FFFF，固定不可配。
- **换行模式**：固定 LF（`\n`）；`.` 不匹配 `\n`；`\r` 是普通字符。
- **匹配语义**：最长匹配优先；等长时显式 `priority` 大者胜；仍并列时声明顺序靠前者胜。
- **空串规则**：能匹配空串的普通词元规则一律拒绝（`input_error` / `EMPTY_MATCH`）。
- **正则子集**：字面量、转义（`\n \t \r \f \v \0 \\`、元字符、`\xHH`、`\uHHHH`）、
  字符类（含 `^` 取补与区间）、连接、`|`、`( )`、`* + ? {m} {m,} {m,n}`、`.`。

## 模块边界与契约

| 模块 | 职责 | 关键文件 |
|---|---|---|
| 语料规范 `app/spec` | 规范结构模型与语义校验（语法、空串、资源） | `models.py` `validation.py` |
| 挖掘内核 `app/kernel` | 正则解析、NFA/DFA、最长匹配词法器、重叠/不可达诊断 | `regex_ast.py` `nfa.py` `dfa.py` `lexer.py` `diagnostics.py` |
| 索引与模型 `app/store` | SQLite 持久化：规范、编译模型、诊断、运行日志 | `repo.py` |
| 查询验证 `app/api` | 请求校验、路由、统一错误契约 | `routes.py` `schemas.py` |

**错误契约**（所有端点统一）：`{"error": {"category", "code", "message", "details"}}`，四类可区分：

| category | HTTP | 含义 | 示例 code |
|---|---|---|---|
| `input_error` | 400 | 输入不合法 | `REGEX_SYNTAX` `EMPTY_MATCH` `LEX_NO_MATCH` `SPEC_NOT_FOUND` |
| `state_conflict` | 409 | 与持久化状态冲突 | `SPEC_EXISTS` `LEXER_NOT_READY` |
| `resource_exhausted` | 413 | 超出资源上限 | `REPEAT_TOO_LARGE` `DFA_TOO_LARGE` `INPUT_TOO_LONG` |
| `computation_failed` | 500 | 内部计算失败 | `INTERNAL` |

**诊断语义**：
- 重叠见证：两规则语言的交集中的最短串（积自动机 0-1 BFS，字符取交集最小码点，结果确定）。
- 不可达规则：`L(R) ⊆ ⋃L(R')`（`R'` 为等长能击败 R 的规则）时 R 永不能胜出，判不可达；
  判定式为 `L(R) ∩ 补(⋃L(R'))` 空性检查（补自动机先完备化）。

## 配置

环境变量（均有默认值，见 `app/config.py`）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `LEXGEN_DB` | `./lexgen.db` | SQLite 路径 |
| `LEXGEN_MAX_RULES` | 64 | 规则数上限 |
| `LEXGEN_MAX_PATTERN_LEN` | 2000 | 模式长度上限 |
| `LEXGEN_MAX_REPEAT` | 1000 | `{m,n}` 上界 |
| `LEXGEN_MAX_NFA_STATES` / `LEXGEN_MAX_DFA_STATES` | 20000 | 自动机规模上限 |
| `LEXGEN_MAX_PRODUCT_STATES` | 200000 | 积自动机规模上限 |
| `LEXGEN_MAX_INPUT_CHARS` | 1000000 | 待词法文本长度上限 |

## 依赖版本（requirements.txt）

```
fastapi==0.141.1
uvicorn==0.54.0
pydantic==2.13.5
httpx==0.28.1
pytest==9.1.1
```

## 从干净目录复现

```bash
cd opp438/b
python3 -m venv .venv && . .venv/bin/activate   # 可选
pip install -r requirements.txt
python3 -m pytest tests/ -q                      # 运行测试
LEXGEN_DB=./lexgen.db python3 -m uvicorn app.main:app --port 8137 &
```

## 请求样例

```bash
# 1. 登记规范（201）
curl -s -X POST localhost:8137/specs -H 'Content-Type: application/json' -d '{
  "name": "keywords", "version": "1",
  "rules": [
    {"name": "IF", "pattern": "if", "priority": 10},
    {"name": "ELSE", "pattern": "else", "priority": 10},
    {"name": "IDENT", "pattern": "[A-Za-z_][A-Za-z0-9_]*"},
    {"name": "WS", "pattern": "[ \\t\\n]+", "skip": true}
  ]}'
# => {"spec_id":1,"run_id":"run-..."}

# 2. 构建词法器并运行诊断（201）
curl -s -X POST localhost:8137/lexers -H 'Content-Type: application/json' -d '{"spec_id": 1}'
# => {"lexer_id":1,...,"diagnostics":{"overlaps":[
#      {"rule_a":"IF","rule_b":"IDENT","witness":"if"}, ...], "unreachable":[...]}}

# 3. 词法执行，词元带原偏移（200）
curl -s -X POST localhost:8137/lexers/1/tokenize -H 'Content-Type: application/json' \
  -d '{"text": "if iffy else"}'
# => {"tokens":[{"type":"IF","start":0,"end":2,"text":"if"},
#               {"type":"IDENT","start":3,"end":7,"text":"iffy"},
#               {"type":"ELSE","start":8,"end":12,"text":"else"}]}

# 4. 诊断与日志重放
curl -s localhost:8137/lexers/1
curl -s localhost:8137/runs/<run_id>/logs
```

## 测试与验证设计

- **具体断言**：关键字/标识符、数字格式（INT/FLOAT/HEX）、注释跳过、同长优先级决胜等
  场景的词元序列与偏移均为手工书写的参考答案，不是被测内核生成。
- **独立核验**：
  - Python `re` 作为独立预言机复核见证归属与词元归属；
  - 暴力枚举更短串证明见证最短性；
  - 测试本地实现的积自动机 BFS（作用于内核导出的 DFA 数据）独立复核交集见证。
- **失败类别**：语法错误、空串规则、词法失败偏移（`input_error`）、重复规范（`state_conflict`）、
  超限重复（`resource_exhausted`）分别断言类别与 HTTP 状态。
- **运行日志**：每次运行分配 `run-<时间戳>-<随机>` 编号，日志含模块、事件、
  关键中间状态（NFA/DFA 状态数、积自动机检查对数等）与判断理由，落库后可按
  `run_id` 经 `GET /runs/{run_id}/logs` 重放。

## 验收记录（如实记录）

在本机（Python 3.12.3，Linux）从干净目录执行：

```
$ python3 -m pytest tests/ -q
65 passed, 1 warning in 0.56s
```

uvicorn 冒烟（`LEXGEN_DB=./smoke.db`，端口 8137）实测结果：

| 步骤 | 结果 |
|---|---|
| `GET /health` | `{"status":"ok"}` |
| 创建规范 keywords v1 | 201，`spec_id=1` |
| 重复创建同名规范 | 409 `state_conflict` / `SPEC_EXISTS` |
| 构建词法器 | 201，`dfa_states=11`，重叠见证 `IF∩IDENT="if"`、`ELSE∩IDENT="else"`，不可达规则 0 |
| 词法 `if iffy else` | 200，词元 `IF(0,2)` `IDENT(3,7)` `ELSE(8,12)` |
| 词法 `if @` | 400 `input_error` / `LEX_NO_MATCH`，`offset=3` |
| 空串规则 `a*` | 400 `input_error` / `EMPTY_MATCH` |
| `GET /runs/{run_id}/logs` | 200，按 seq 返回带中间状态与理由的日志条目 |

已知限制：DFA 未做最小化（正确性不受影响）；诊断按规则对全量计算，规则数受
`LEXGEN_MAX_RULES` 约束；日志与模型存于单一 SQLite 文件，无并发写优化。
