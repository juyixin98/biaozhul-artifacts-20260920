# 验收记录（VERIFICATION）

本文件如实记录在干净目录中的实际执行结果。执行环境：

- OS：Linux 6.8.0-90-generic (x86_64)
- Python：3.12.3
- 依赖（`pip show` 实测版本，已固定在 `requirements.txt`）：
  fastapi 0.141.1 · uvicorn 0.54.0 · pydantic 2.13.5 ·
  starlette 1.7.0 · httpx 0.28.1 · pytest 9.1.1 · pytest-cov 7.1.0
- 日期：2026-09-28

## 1. 测试命令与结果

```bash
$ python3 -m pytest --cov=app --cov-report=term-missing
34 passed, 1 warning in 2.13s
```

唯一警告来自 starlette 0.54 对 `httpx` TestClient 的弃用提示，与本工程逻辑无关。

覆盖率（总计 95%，满足 ≥80% 要求）：

| 模块 | 语句 | 未覆盖 | 覆盖率 |
|---|---|---|---|
| app/config.py | 18 | 0 | 100% |
| app/corpus/spec.py | 50 | 2 | 96% |
| app/errors.py | 31 | 1 | 97% |
| app/index/db.py | 37 | 0 | 100% |
| app/index/models.py | 15 | 0 | 100% |
| app/kernel/ast_nodes.py | 56 | 2 | 96% |
| app/kernel/compiler.py | 35 | 0 | 100% |
| app/kernel/dfa.py | 88 | 3 | 97% |
| app/kernel/diagnostics.py | 63 | 0 | 100% |
| app/kernel/lexer.py | 46 | 0 | 100% |
| app/kernel/nfa.py | 97 | 10 | 90% |
| app/kernel/regex_parser.py | 174 | 15 | 91% |
| app/main.py | 90 | 10 | 89% |
| app/query/validation.py | 37 | 3 | 92% |
| app/runlog.py | 15 | 0 | 100% |
| **TOTAL** | **852** | **46** | **95%** |

未覆盖行主要为冷僻正则语法错误分支和 `COMPUTATION_FAILURE` 兜底分支。

### 34 个用例（全部通过）

- 关键字与标识符：`if ifx else1 while` 的具体词元与偏移；
  KW/ID 最短重叠见证为 `if`。
- 数字格式：`42 3.14 0xff 007` → INT/FLOAT/HEX/INT 的具体词元与偏移；
  `7.` 返回已成功前缀并把失败定位到 offset 1 的 `.`。
- 注释：`if x // if else\n42` 中注释内关键字不被词法化；全部词元用
  `start/end` 切片可无缝拼回原文。
- 同长匹配：显式优先级翻转（LIT_AB↔PAIR 各胜一次）、缺省优先级由声明顺序裁决、
  重叠见证 `ab`、PAIR 的最短取胜串为 `aa`。
- 重叠/不可达（独立 oracle 核验）：A/B=`acd`、A/C=`abd`、B/C=`ad`、C/KW=`if`；
  KW 判定为不可达；每个见证均通过 `tests/oracle.py` 的
  Python `re` 成员判定 + 穷举最短性核验。
- 固定语义：`.` 排除 LF 但接受 CR；`[^x]` 接受换行；`\d\w\s` 仅 ASCII
  （U+0661 不算数字）；Unicode 字面量 `☃☃` 偏移正确。
- 错误分类：空串规则（`a* (ab)? x{0,3} (a|) a*b?` 共 5 例）→ 400 INPUT_ERROR；
  语法错/非法区间/重名 → 400；规则集重名 → 409 STATE_CONFLICT；
  `a{5000}`、超长输入、规则数超限、NFA 状态上限 → 413 RESOURCE_EXHAUSTED；
  未知资源 → 404 INPUT_ERROR。
- 运行日志：成功编译的阶段序列为
  `validate → parse×N → nfa → dfa → diagnostics → stored`，
  保留 NFA/DFA 状态数、见证与取胜串等中间状态；错误运行的日志中
  类别字段可区分 INPUT_ERROR 与 RESOURCE_EXHAUSTED。
- 内核直测：`(ab|a)(c*)+[0-9]{2}` 的接受/拒绝样本与 Python `re` 交叉一致。

## 2. 合成夹具实测

把 `fixtures/rulesets/*.json` 逐个 POST 到真实 ASGI 应用：

| 夹具 | HTTP | 结果 |
|---|---|---|
| keywords_identifiers.json | 201 | overlaps=1（KW/ID=`if`），unreachable=[] |
| numbers.json | 201 | overlaps=0，unreachable=[] |
| comments.json | 201 | overlaps=1（KW/ID），unreachable=[] |
| ties.json | 201 | overlaps=1（LIT_AB/PAIR=`ab`） |
| overlap_unreachable.json | 201 | overlaps=4，unreachable=['KW'] |
| invalid_empty_match.json | 400 | INPUT_ERROR：rule 'BAD' can match the empty string |
| resource_blowup.json | 413 | RESOURCE_EXHAUSTED：NFA state limit 4096 exceeded |

## 3. 端到端演示（进程内）

`python3 scripts/smoke_demo.py` 实测：编译返回 4 对重叠最短见证与不可达规则
KW；词法输入 `"acd if"` 输出 `A("acd",0,3) WS(" ",3,4) C("if",4,6)`
——`if` 因 KW 不可达而归 C；空串规则请求返回 400，且两次请求各自的
run_id 均可经 `GET /api/runs/{id}` 重放。

## 4. 真实 HTTP 服务实测

```bash
$ LEXER_DB_PATH=data/demo/http.db uvicorn app.main:app --port 8123
$ curl -s localhost:8123/api/health
{"status":"ok"}
```

创建 KW/ID/WS 规则集后词法 `"iffy if"`：

```json
{"tokens": [
  {"rule": "ID", "text": "iffy", "start": 0, "end": 4},
  {"rule": "WS", "text": " ",    "start": 4, "end": 5},
  {"rule": "KW", "text": "if",   "start": 5, "end": 7}],
 "error": null}
```

（`iffy` 比关键字 `if` 长，故归 ID——最长匹配优先的直接证据。）

## 5. 独立核验说明

`tests/oracle.py` 不 import 任何 `app.kernel` 代码：它用 Python 标准库 `re`
（`re.ASCII`，与语料固定的字符语义对齐）判定见证是否同时被两条模式接受
（即位于两语言交集内），并在模式字符字母表上穷举所有更短串确认最短性与
字典序唯一。内核答案与穷举答案逐字断言相等，参考答案不是由被测核心自身生成。

## 6. 复现步骤

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pytest --cov=app --cov-report=term-missing   # 预期 34 passed, 95% 覆盖
python3 scripts/smoke_demo.py                # 端到端演示
uvicorn app.main:app --port 8000             # 启动 HTTP 服务
```
