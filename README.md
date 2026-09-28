# lcs-batch — 多文档最长公共子串批检索

基于**广义后缀数组 + LCP** 的多文档公共连续片段查询服务。语料为任意字节串
（支持二进制），查询以「至少覆盖 K 个不同文档」为条件，返回全部并列最长
候选及其在原始文档中的偏移（含重叠出现）。

## 模块划分

| 模块 | 职责 |
|---|---|
| `lcs_batch/corpus.py` | 语料规范：文档模型、分隔符安全编码、原始偏移映射 |
| `lcs_batch/kernel.py` | 挖掘内核：后缀数组（倍增法）、LCP（Kasai）、RMQ 稀疏表、覆盖度滑窗与并列枚举 |
| `lcs_batch/index_store.py` | 索引与模型：SQLite 持久化文档/数组/元数据 + 查询审计日志 |
| `lcs_batch/validation.py` | 查询与语料校验，带稳定失败类别（`ErrorCategory`） |
| `lcs_batch/service.py` | 编排：建索引、批查询、可解释结果渲染 |
| `lcs_batch/api.py` | FastAPI HTTP 接口、请求身份关联中间件 |
| `lcs_batch/config.py` | 环境变量配置与内核版本号 |
| `tests/` | 独立测试（含不依赖内核的穷举对照 oracle） |

## 关键设计取舍

### 1. 分隔符不可与正文冲突（编码层面保证，而非约定）

文档是字节串，无法保留某个"安全字节"做分隔符。因此拼接流不是字节串而是
**整数符号流**：正文字节 `b` 映射为符号 `b`（0–255），第 `i` 篇文档之后追
加唯一分隔符符号 `256 + i`。

- 分隔符 ≥ 256，**不可能**与任何正文字节冲突（`tests/test_corpus.py` 用
  含全部 256 个字节值的文档验证不相交）；
- 每个分隔符全局唯一，因此 LCP 匹配永远不会"跨过"分隔符 —— 匹配不跨文
  档边界是由编码不变式保证的，而不是靠事后过滤。

### 2. 覆盖度按"不同文档数"计量

`min_docs` 表示候选子串必须出现在至少 K 个**不同文档**中，与出现次数无
关（`test_coverage_counts_documents_not_occurrences`：同一文档内出现 3 次
不计入覆盖度）。`min_docs` 下限为 2：K=1 退化为"最长文档"，不属于公共子
串语义，校验层直接拒绝（`INVALID_MIN_DOCS`）。

### 3. 同长度候选稳定排序

内核两遍扫描：滑窗求最大长度 `L*`，再枚举所有内部 LCP ≥ `L*` 的极大 SA
区间 —— 区间与不同的最长子串一一对应，因此**并列候选被完整枚举**。输出
按 `(子串字节序, 出现位置列表)` 排序，与发现顺序无关，可复现
（`test_stable_order_is_byte_order_not_discovery_order`）。

### 4. 原始偏移与重叠

每个候选返回全部出现位置 `(doc_id, offset)`，偏移为原文档内字节偏移；
重叠出现（如 `"aa"` 在 `"aaaa"` 的 0/1/2 偏移）全部保留。

### 5. 可解释性

- 每个响应携带 `request_id`（客户端可经 `X-Request-ID` 头或请求体指定）
  与 `index_version`（内核算法版本，持久化在索引元数据中，版本不匹配拒
  绝加载）；
- 批响应中失败项单列于 `failures`，不确定结论（截断、无公共子串）单列
  于 `uncertainties`，与硬错误区分；
- 每次查询写入 SQLite `query_log` 审计表（`GET /v1/audit/recent` 可查）；
- 日志为结构化 JSON，携带 `request_id` / `query_id` / `index_version` /
  `duration_ms` / `status` / `category`。

## 本地启动

```bash
pip install -r requirements.txt          # Python 3.12
uvicorn lcs_batch.api:app --port 8000    # 默认 SQLite 文件 ./lcs_index.db
# 可选环境变量：LCS_DB_PATH, LCS_MAX_DOCUMENTS, LCS_MAX_DOCUMENT_BYTES,
#               LCS_MAX_TOTAL_SYMBOLS, LCS_DEFAULT_MAX_CANDIDATES
```

## 示例请求

建索引（文档内容一律 base64，支持任意二进制）：

```bash
curl -s -X POST localhost:8000/v1/index/build \
  -H 'Content-Type: application/json' -H 'X-Request-ID: req-demo-build' \
  -d '{"documents": [
        {"doc_id": "alpha", "content_b64": "YWJjWFlaZGVm"},
        {"doc_id": "beta",  "content_b64": "YWJjUVFkZWY="},
        {"doc_id": "gamma", "content_b64": "AP9hYmMA"}
      ]}'
# => {"request_id": "req-demo-build", "index_version": "gsa-lcp/1.0",
#     "doc_count": 3, "symbol_count": 26, ...}
```

批查询（一个批次内成功与失败互不影响）：

```bash
curl -s -X POST localhost:8000/v1/query -H 'Content-Type: application/json' \
  -d '{"request_id": "req-demo-query",
       "queries": [
         {"query_id": "all-three", "min_docs": 3},
         {"query_id": "any-pair",  "min_docs": 2},
         {"query_id": "bad",       "min_docs": 1}
       ]}'
```

`any-pair` 返回两个并列最长候选（`616263`="abc" 覆盖 3 文档，
`646566`="def" 覆盖 2 文档），每个候选带各文档原始偏移；`bad` 项为
`"status": "error"`，`error.category == "INVALID_MIN_DOCS"`，同时汇总在
顶层 `failures` 数组。

## 接口一览

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 存活与内核版本 |
| `POST /v1/index/build` | 校验语料 → 编码 → 建 SA/LCP → 持久化 SQLite |
| `POST /v1/query` | 批查询：`queries[]` 每项 `{query_id, min_docs, max_candidates}` |
| `GET /v1/index/info` | 索引元数据（版本、文档数、各文档 SHA-256） |
| `GET /v1/audit/recent` | 最近查询审计记录 |

## 失败类别（稳定枚举，测试逐项断言）

`EMPTY_CORPUS` `TOO_MANY_DOCUMENTS` `DUPLICATE_DOC_ID` `INVALID_DOC_ID`
`EMPTY_DOCUMENT` `DOCUMENT_TOO_LARGE` `CORPUS_TOO_LARGE`
`INVALID_CONTENT_ENCODING` `INVALID_QUERY_ID` `INVALID_MIN_DOCS`
`MIN_DOCS_EXCEEDS_CORPUS` `INVALID_MAX_CANDIDATES` `EMPTY_BATCH`
`INDEX_NOT_FOUND` `INDEX_CORRUPT` `KERNEL_VERSION_MISMATCH`

## 测试

```bash
python3 -m pytest                 # 45 个用例
python3 -m pytest --cov=lcs_batch # 覆盖率（当前 97%）
```

测试设计要点：

- **穷举对照**（`test_exhaustive.py`）：参考答案由测试内独立的暴力
  oracle（枚举全部子串 + `bytes.find` 重叠扫描）生成，与被测内核不共享
  任何代码；150 组随机语料（含 `0x00`/`0xFF`）+ 60 组高重复语料，逐候
  选比对长度、子串集合、覆盖文档集合与每文档偏移列表；
- **具体断言**：重复单文档、多文档边界（`"aa"+"aa"` 不得产生 `"aaa"`）、
  二进制字节、并列最长、重叠偏移、覆盖度语义均断言具体返回值；
- **失败类别**：校验与 API 层断言 `error.category` 的具体枚举值，而非
  "接口报错即可"。

### 最近执行记录（2026-09-27，Python 3.12.3）

- `45 passed, 0 failed, 0 skipped`，总覆盖率 97%（最低模块 91%）。
- 非阻塞告警 1 条：starlette TestClient 提示 httpx 弃用（
  `StarletteDeprecationWarning`），仅影响测试客户端，不影响被测代码。
- 无跳过、无预期失败（xfail）项。

## 支持范围与限制

- 语料为**不可变快照**：重新 build 即整体替换索引（SQLite 事务内完成）；
  不支持增量追加文档。
- 后缀数组为纯 Python 倍增法 O(n log n)，配合默认
  `LCS_MAX_TOTAL_SYMBOLS=2_000_000` 的规模上限适合本地/评审场景；更大语
  料应换用 SA-IS 或外部构建器（内核构造函数接受外部 SA/LCP 数组，持久化
  格式已分离）。
- 单节点 SQLite；并发安全依赖服务内锁 + SQLite 事务，不面向多进程写入。
- 子串以字节hex返回，不做文本编码假设；如需 UTF-8 视图由调用方解码。
