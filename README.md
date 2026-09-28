# collsvc — 固定区域与 Unicode 版本的字符串排序 / 范围检索服务

基于成熟排序库 **ICU（PyICU 74.2，Unicode 15.1）**，用 **FastAPI + SQLite**
实现的多模块后端。它同时持久化**原文与 ICU 排序键**，把排序强度、数字排序、
大小写等选项**绑定到索引版本**，保证规则升级必须重建索引、旧游标绝不混入，
范围边界一律用**排序键 BLOB** 而不是 UTF-8 字节比较。

- 排序、范围、前缀全部由 ICU 真实计算，无硬编码演示；
- 规范等价（NFC/NFD）原文不同时，排序键相同但**文档身份不丢**；
- 前缀检索对「数字排序 / IDENTICAL 强度 / 区域收缩规则（da `aa`、cs `ch`）」
  自动降级为全扫描 + 库级精确谓词，并在响应与日志中**显式标注**；
- 每个响应都带 `trace`：请求 id、索引版本、有序步骤、失败类别、不确定结论，
  同时以单行 JSON 日志落盘，失败可逐步复盘。

## 环境要求

- Python ≥ 3.10
- 系统 ICU 与 PyICU（本仓库开发环境：ICU 74.2 / Unicode 15.1）

```bash
pip install -r requirements.txt
# Debian/Ubuntu 若缺系统库：sudo apt-get install libicu-dev
```

## 一分钟上手

```bash
# 1) 建索引（合成夹具 data/corpus/demo.json，无任何真实业务数据）
PYTHONPATH=src python3 scripts/reindex.py demo --locale en_US --strength 3

# 2) 起服务
PYTHONPATH=src python3 -m collsvc.api.main          # http://127.0.0.1:8000

# 3) 建库（或直接走 HTTP）
curl -s -X POST localhost:8000/admin/index/build \
  -H 'Content-Type: application/json' \
  -d '{"corpus_name":"demo","options":{"locale":"en_US","strength":3}}'
```

常用查询：

```bash
# 排序后的完整列表
curl -s -X POST localhost:8000/query/sorted -H 'Content-Type: application/json' \
  -d '{"options":{"locale":"en_US","strength":3},"limit":100}'

# 值范围：边界按排序键比较，étude / élève 落在 [e, f)
curl -s -X POST localhost:8000/query/range -H 'Content-Type: application/json' \
  -d '{"options":{"locale":"en_US","strength":3},"low":"e","high":"f"}'

# 校对前缀（强度 1 时忽略大小写与重音）
curl -s -X POST localhost:8000/query/prefix -H 'Content-Type: application/json' \
  -d '{"options":{"locale":"en_US","strength":1},"prefix":"COTE","match":"collation"}'

# 土耳其语排序
curl -s -X POST localhost:8000/query/sorted -H 'Content-Type: application/json' \
  -d '{"options":{"locale":"tr_TR","strength":3}}'
```

## 运行测试（真实命令与结论）

```bash
$ python3 -m pytest tests/ -q
134 passed, 1 warning in 2.32s

$ python3 -m pytest tests/ -q --cov=src/collsvc
TOTAL  989 stmts ...  Cover 92%
```

测试分三层：

| 层 | 文件 | 断言内容 |
|----|------|----------|
| 引擎 golden | `tests/unit/test_engine_golden.py` | 逐字节比对固化的 ICU 排序键、重音/数字/大小写/土耳其语序；期望值来自**独立探测脚本**和**全新构造的 ICU collator**，不是被测核心生成 |
| 独立 oracle | `tests/unit/test_query_oracle.py` | oracle 自建 ICU collator、用纯 Python 重算答案，与索引/SQL 实现逐项比对；断言**具体结果**与**具体失败类别**（版本冲突、游标串模式、降级原因） |
| 属性模糊 | `tests/unit/test_collation_properties.py` | 固定种子随机串，验证 seek 区间召回、谓词与库一致、稳定性 |
| 其余单元 | `test_mining.py / test_cursor_version.py / test_boundaries.py` | 语料规范、挖掘内核、游标/版本、存储边界 |
| HTTP 集成 | `tests/integration/test_http_api.py` | 真实 ASGI + 磁盘 SQLite，断言 JSON 结果、状态码、错误类别、trace |

> Golden 期望值是一次性用只依赖 PyICU 的独立脚本录制的
> （`tests/fixtures/golden.py`）。ICU 升级若改变排序键，这些测试会先红，
> 且索引版本号内嵌 ICU 版本而随之改变，强制重建。

## 复现一次失败：版本冲突 + 降级

```text
# 数字前缀：数字 run 被合并成单个权重，seek 无法保证召回 → 全扫描
{"rows":[file1, file2, file02, file10, file20], "degraded": true}
trace.uncertainties[0].detail.reason = "numeric_collation_merges_digit_weights"

# 规则改变后重建，再用旧游标翻页
error_category = "INDEX_VERSION_CONFLICT"
reason: cursor was issued for index 'idx_v1_e112...', current index is 'idx_v1_61a8...'
```

失败响应与日志共享同一 `request_id`，`trace.steps` 给出关键步骤、所用索引版本和
处理位置（如 `query/service.range_between`），`trace.failures` 单列失败原因，
`trace.uncertainties` 单列「正确但非索引 seek 保证」的降级结论。

## API 摘要

| 方法 路径 | 说明 |
|-----------|------|
| `GET /health` | ICU / Unicode 版本 |
| `POST /admin/index/build` | 用夹具建/重建索引（选项写入版本） |
| `GET /admin/index/meta` | 查看已建索引的版本与元数据 |
| `POST /query/sorted` | 排序列表（keyset 分页） |
| `POST /query/range` | 两端点之间的值范围（排序键 BLOB） |
| `POST /query/prefix` | 前缀检索，`match=collation` 或 `text` |

选项字段：`locale`、`strength`（1 基字母 / 2 含重音 / 3 含大小写 / 4 / 15 逐字节）、
`numeric`、`case_first`（default/upper/lower）。规范化始终开启。

错误类别：`INDEX_VERSION_CONFLICT(409)`、`INVALID_CURSOR(400)`、
`UNSUPPORTED_LOCALE(400)`、`INVALID_CORPUS_SPEC(400)`、`INDEX_NOT_BUILT(409)`、
`CORPUS_NOT_FOUND(404)`、`INVALID_ARGUMENT(400)`。

## 模块划分

见 [docs/design.md](docs/design.md)。核心分工：语料规范（`corpus`）、
挖掘内核（`mining`）、索引与模型（`index`）、查询与独立验证（`query`），
另有独立测试（`tests`）与配置（`config.py`）。

## 配置（环境变量，均有本地默认值）

| 变量 | 默认 |
|------|------|
| `COLLSVC_DB_PATH` | `data/collsvc.db`（按选项派生的索引放在 `data/indexes/`） |
| `COLLSVC_CORPUS_DIR` | `data/corpus` |
| `COLLSVC_PAGE_SIZE_DEFAULT` / `_MAX` | `50` / `200` |
