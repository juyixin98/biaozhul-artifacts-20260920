# minidfa — 不可变有序词典的最小无环确定自动机（DAFSA）

从有序词表增量构建**最小无环确定有限自动机**（Daciuk–Mihov–Watson–Watson 算法），
支持词典成员查询与前缀计数，自动机持久化到 SQLite，加载时强制引用校验。
提供 FastAPI 服务入口与本地演示脚本。

## 行为契约

1. **状态等价**：等价签名 = `(终结标记, 排序后的全部转移 (符号, 子状态规范身份))`。
   终结标记与转移共同决定等价类，绝不按子节点数量合并
   （见 `minidfa/builder.py` 的 `_State.signature`，
   针对性测试：`tests/test_minimality.py::test_transitions_matter` /
   `test_final_flag_matter`）。
2. **有序输入**：构建要求字典序（Unicode 码位）升序且无重复。
   - `strict`（默认）：乱序 → `UNSORTED_INPUT`，重复 → `DUPLICATE_WORD`，明确拒绝并给出位置；
   - `normalize`：先排序去重再构建。
3. **空词规则（固定）**：空词 `""` 是合法词；字典序最小，必须排首位；每个词典至多一次；
   接受语义体现为初始状态带终结标记。空语料合法，产生单个非终结状态，拒绝一切词。
4. **持久化引用校验**：加载时校验——转移源/目标必须存在（`DANGLING_REFERENCE`）、
   状态图无环（`CYCLE_DETECTED`）、全部状态从初始状态可达（`UNREACHABLE_STATE`）、
   符号为单字符（`INVALID_SYMBOL`）。任一违反即拒绝加载。

## 工程结构

```
minidfa/
  config.py       配置层：Settings（环境变量可覆盖）、日志
  corpus.py       语料规范层：排序/去重/词法校验、输入指纹
  builder.py      挖掘内核：增量最小化构建
  automaton.py    索引与模型层：冻结自动机、contains / prefix_count / iter_words
  persistence.py  持久化层：SQLite 存取 + 引用校验
  service.py      编排层：run_id 诊断日志、构建报告
  api.py          查询验证层：FastAPI 入口与错误映射
tests/
  reference_trie.py  独立 Trie 参考实现（不共享被测核心代码）
  test_minimality.py 手工推导的最小状态数
  test_corpus.py     排序契约 / 空词规则 / 重复词 / 空集合
  test_vs_trie.py    与 Trie 交叉验证接受语言与前缀计数（含随机语料）
  test_persistence.py 往返一致性 + 环/悬空引用/不可达状态拒绝
  test_api.py        HTTP 错误语义
  test_service.py    诊断日志（run_id、指纹、版本、计算步骤）
scripts/demo.py   本地演示
```

## 错误语义

所有领域错误携带稳定 `category`，API 返回结构化 JSON，绝不把异常统一包装为成功：

| category | HTTP | 含义 |
|---|---|---|
| `UNSORTED_INPUT` | 422 | 严格模式输入未升序，`detail` 含出错位置与相邻词 |
| `DUPLICATE_WORD` | 422 | 严格模式输入含重复词 |
| `INVALID_WORD` | 422 | 非字符串或含 NUL 字符 |
| `CORPUS_ERROR` | 422 | 其他语料错误（如超过 `max_words`） |
| `AUTOMATON_NOT_FOUND` | 404 | 自动机不存在 |
| `DANGLING_REFERENCE` | 500 | 持久化数据含悬空引用 |
| `CYCLE_DETECTED` | 500 | 持久化数据含环 |
| `UNREACHABLE_STATE` | 500 | 持久化数据含不可达状态 |
| `INVALID_SYMBOL` | 500 | 转移符号非单字符 |
| `INTERNAL` | 500 | 未分类异常（显式 500，不吞错） |

## 诊断

每次构建分配 `run_id`，日志携带：run_id、输入指纹（规范化词表的 sha256 前 16 位）、
minidfa 版本、Python 版本、各计算步骤（语料校验 → 最小化状态/边数 → 持久化耗时）。
测试 `tests/test_service.py` 断言这些字段确实出现在日志中。

## 复现步骤

```bash
pip install -r requirements.txt   # fastapi / uvicorn / pytest / httpx

# 1. 运行测试（49 个用例，实际执行并报告结果）
python3 -m pytest tests/ -v

# 2. 本地演示：构建 → 持久化 → 查询 → 错误语义
python3 scripts/demo.py ./demo.db

# 3. 启动服务（默认库文件 ./minidfa.db，可用 MINIDFA_DB_PATH 覆盖）
uvicorn minidfa.api:app --port 8000

# 4. 调用示例
curl -X POST localhost:8000/automata \
  -H 'Content-Type: application/json' \
  -d '{"name": "dict", "words": ["ape", "apple", "band"]}'
curl 'localhost:8000/automata/dict/contains?word=apple'
curl 'localhost:8000/automata/dict/prefix_count?prefix=ap'
curl -X POST localhost:8000/automata \
  -H 'Content-Type: application/json' \
  -d '{"name": "bad", "words": ["b", "a"]}'        # → 422 UNSORTED_INPUT
```

## 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `MINIDFA_DB_PATH` | `./minidfa.db` | SQLite 库文件路径 |
| `MINIDFA_LOG_LEVEL` | `INFO` | 日志级别 |
| `MINIDFA_MAX_WORDS` | `1000000` | 单自动机词数上限 |

## 验证方法说明

- **最小性**：`tests/test_minimality.py` 使用手工推导的状态数（如 `{ab,abc,gc}` → 5 态，
  忽略终结标记的错误合并会得到 4 态且错误接受 `g`）。
- **正确性**：`tests/test_vs_trie.py` 用独立实现的 Trie（`tests/reference_trie.py`，
  与被测核心零共享代码）交叉比对接受语言枚举与全部前缀计数，覆盖共享后缀、
  词互为前缀、重复词、空集合及 4 个种子的随机语料。
- **持久化**：`tests/test_persistence.py` 通过手工篡改 SQLite 制造悬空引用、环、
  不可达状态，断言加载以具体类别拒绝。
