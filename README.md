# multi-doc-lcs — 多文档最长公共连续片段批检索

基于**广义后缀数组 + LCP** 的多文档最长公共子串（连续字节片段）批量查询服务。
Python 3.12 / FastAPI / SQLite，全部本地运行，语料为本地合成夹具，无任何外部账号依赖。

## 支持范围

- 文档为**原始字节**（二进制安全，含 `0x00`–`0xFF` 全部取值），经 base64 提交。
- 查询语义：返回**至少覆盖 `min_docs` 个不同文档**的最长公共连续片段。
  覆盖度按**不同文档数**计量，绝不按出现次数计量。
- 同长度候选全部返回，按**原始字节字典序**稳定排序（与文档上传顺序无关，可复现）。
- 每个候选返回：片段内容（base64 + hex）、覆盖文档集合、**原始字节偏移**的出现位置列表，
  **重叠出现全部保留**。
- 批量查询：一次请求多个查询规格；单个查询失败只影响自身，携带类型化失败类别。
- 语料与索引持久化于 SQLite，服务重启后可直接复用（索引格式版本不匹配时拒绝加载而非静默错读）。

## 关键取舍

1. **分隔符与正文不可能冲突（构造性保证，非约定）**
   拼接流建立在整数符号空间上：正文字节 `b` 映射为符号 `b`（0..255），
   第 `i` 篇文档后的分隔符为符号 `256 + i`。分隔符与全部 256 个字节值**不相交**，
   且每个文档槽位的分隔符唯一 —— 唯一符号不可能出现在任何长度 ≥1 的公共前缀中，
   因此**匹配跨越文档边界在构造上不可能**，无需运行时检查兜底
   （`locate` 仍对分隔符位置做防御性跳过）。
2. **后缀数组用前缀加倍法（O(n log n) 轮排序），LCP 用 Kasai。**
   纯 Python 实现，适合中小规模语料（默认上限：512 篇 / 8 MB 总量，可用环境变量调整）。
   未引入 SA-IS 等线性构造，是为可读性与零本地依赖的取舍。
3. **覆盖度扫描是 SA 上的滑动窗口**：窗口仅在覆盖 ≥ `min_docs` 个不同文档时 admissible，
   窗口值为内部最小 LCP（单调队列维护）；单后缀退化窗口取其**文档内剩余长度**，
   保证 k=1 时也不会越出文档末端。
4. **候选收集按 LCP ≥ L 的极大区间切分**，去重后按字节排序 —— 这是并列最长稳定序的来源。
5. **索引落盘 + 内存缓存**：`array('q')` 序列化符号流/SA/LCP 到 SQLite；
   进程内字典缓存避免重复反序列化。单节点设计，无并发写扩展诉求。

## 模块划分

| 模块 | 职责 |
|---|---|
| `app/corpus.py` | 语料规范：base64 解码、文档校验、符号流拼接（分隔符布局） |
| `app/kernel.py` | 挖掘内核：后缀数组、Kasai LCP、覆盖度滑窗、候选切分、出现位置二分 |
| `app/index.py` | 索引组装与查询期挖掘（`Index` / `mine_longest` / `locate`） |
| `app/store.py` | SQLite 持久化（语料、文档、序列化索引、格式版本） |
| `app/validation.py` | 查询参数校验（`min_docs` 范围、候选上限），独立于 HTTP 层 |
| `app/models.py` | Pydantic 请求/响应模型 |
| `app/main.py` | FastAPI 端点、请求身份中间件、逐查询执行与失败归类 |
| `app/logging_setup.py` | 日志：contextvar 携带 request_id 与应用版本 |
| `app/config.py` | 配置（环境变量可覆盖）与索引格式版本 |

## 本地启动

```bash
pip install -r requirements.txt
python3 -m app.main            # 监听 127.0.0.1:8000，数据库 ./lcs_service.db
# 或： LCS_DB_PATH=/tmp/lcs.db uvicorn app.main:app --port 8000
```

## 示例请求

```bash
# 1. 创建语料（文档为 base64 的原始字节）
curl -s -X POST http://127.0.0.1:8000/corpora -H 'content-type: application/json' -d '{
  "name": "demo",
  "documents": [
    {"doc_id": "alpha", "content_b64": "dGhlIHF1aWNrIGJyb3duIGZveCBqdW1wcw=="},
    {"doc_id": "beta",  "content_b64": "dGhlIHF1aWNrIGJyb3duIGRvZyBzbGVlcHM="},
    {"doc_id": "gamma", "content_b64": "c2F5IHRoZSBxdWljayBicm93biB0aGluZw=="}
  ]}'
# => {"corpus_id": "...", "doc_count": 3, ...}

# 2. 批量查询（min_docs 为“至少覆盖多少不同文档”）
curl -s -X POST http://127.0.0.1:8000/corpora/<corpus_id>/queries \
  -H 'content-type: application/json' -H 'x-request-id: my-trace-1' -d '{
  "queries": [
    {"query_id": "all-three", "min_docs": 3},
    {"query_id": "any-two",   "min_docs": 2, "max_candidates": 8},
    {"query_id": "too-many",  "min_docs": 9}
  ]}'
```

响应要点：`results[*].status` 为 `ok` / `no_result` / `error`；
失败查询单列 `failure_category`（如 `INVALID_MIN_DOCS`）与 `detail`；
`ok` 查询给出 `length`、稳定排序的 `candidates`（含 `doc_coverage` 与
逐文档 `occurrences` 原始偏移）。`x-request-id` 请求头会被回显并贯穿日志。

## 可解释性

- 每个请求有 `request_id`（可经 `x-request-id` 指定，否则自动生成），
  出现在响应体、响应头与每条日志中。
- 日志按 `step=` 标记关键步骤：`corpus.validated`、`index.built`（含符号数与耗时）、
  `index.loaded`、`query.executed`（含 min_docs/长度/候选数/耗时）、
  `query.rejected`（含失败类别）、`query.empty`（无结果的不确定结论单列）。
- 每条日志携带应用版本；响应携带 `app_version` 与 `index_version`。

## 失败类别

`INVALID_BASE64` · `EMPTY_DOCUMENT` · `EMPTY_DOCUMENT_ID` · `DUPLICATE_DOC_ID` ·
`DOCUMENT_TOO_LARGE` · `TOO_MANY_DOCUMENTS` · `CORPUS_TOO_LARGE` ·
`CORPUS_NOT_FOUND`(404) · `INVALID_MIN_DOCS` · `INVALID_MAX_CANDIDATES` ·
`EMPTY_QUERY_BATCH` · `INDEX_CORRUPT` · `INTERNAL`

## 测试

```bash
python3 -m pytest tests/ -q
python3 -m pytest tests/ -q --cov=app --cov-report=term-missing --cov-fail-under=80
```

当前结果：**85 passed，覆盖率 97%**（门槛 80%）。无跳过、无预期失败项。

测试要点（参考答案均非由被测核心生成）：

- `tests/brute_force.py`：**独立的穷举参考实现**（不 import 内核），
  按长度递减枚举全部子串并统计不同文档覆盖数。
- `tests/test_bruteforce_crosscheck.py`：60+ 组随机短文本（含二进制字节），
  逐一比对长度、候选集合、**文档覆盖集合**与含重叠的出现位置多重集。
- `tests/test_single_document.py`：重复单文档（k=1 全篇、同篇双副本、内部重叠重复）。
- `tests/test_boundaries.py`：分隔符与全字节域不相交、逆序文档、首尾相接文档，
  并逐偏移断言 `content[offset:offset+len] == candidate`。
- `tests/test_binary.py`：0x00–0xFF 全量字节、边界字节、索引落盘重载一致性。
- `tests/test_ties.py`：并列最长候选全部返回、字节序稳定、与文档顺序无关。
- `tests/test_validation.py`：各类校验失败断言到具体失败类别。
- `tests/test_api.py`：端到端（期望值由测试内 `bytes.find` 直接推导）、
  请求身份回显、批量中单查询失败隔离、重启后索引一致。

## 已知限制

- 单机单进程；索引缓存为进程内字典，多实例部署需各自加载。
- 前缀加倍构造对超大语料（>8 MB 默认上限）不是最优；需要更大规模时应换 SA-IS。
- 出现位置每候选上限 `LCS_OCCURRENCE_CAP`（默认 1000），超出置 `occurrences_truncated=true`。
