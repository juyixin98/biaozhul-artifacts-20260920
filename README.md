# collationsvc — 固定区域与 Unicode 版本的字符串排序和范围检索服务

基于 **PyICU**（成熟排序库，ICU 74.2）的排序键生成，**FastAPI** 提供接口，
**SQLite** 持久化索引。排序键与原文同时保存；强度、数字排序、大小写选项
绑定索引版本；规范等价但原文不同的字符串保留各自身份；规则升级必须重建
索引，旧游标一律拒绝；范围边界一律用排序键比较，绝不回退 UTF-8 字节序。

## 模块划分

| 模块 | 职责 |
|------|------|
| `app/config.py` | 排序规则（locale/strength/numeric/case_first）与指纹；环境变量配置 |
| `app/corpus.py` | 语料规范：载入、校验（CORPUS_ERROR）、身份保留、等价簇报告 |
| `app/kernel.py` | 挖掘内核：唯一直接接触 ICU 的模块，排序键与稳定全序 |
| `app/index.py` | 索引与模型：SQLite 落库、版本管理、全量重建 |
| `app/query.py` | 查询验证：游标编解码、范围反转检测、分页编排 |
| `app/api.py` | FastAPI 接口、请求身份关联、可解释错误与日志 |
| `app/errors.py` | 失败类别：VALIDATION_ERROR / RANGE_INVERSION / INDEX_VERSION_CONFLICT / STALE_CURSOR / INDEX_NOT_BUILT / ENTRY_NOT_FOUND / CORPUS_ERROR / INTERNAL |

## 首次运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # PyICU 需要系统 libicu-dev
.venv/bin/python -m pytest                  # 跑测试（见下方真实输出）
.venv/bin/python -m uvicorn app.api:app --port 8000
```

启动配置（环境变量）：

| 变量 | 默认 | 说明 |
|------|------|------|
| `COLLATION_LOCALE` | `en_US` | 固定区域，如 `tr_TR` |
| `COLLATION_STRENGTH` | `tertiary` | primary/secondary/tertiary/quaternary/identical |
| `COLLATION_NUMERIC` | `false` | 数字片段排序（file2 < file10） |
| `COLLATION_CASE_FIRST` | `off` | off/upper_first/lower_first |
| `COLLATION_DB` | `collation.db` | SQLite 路径 |
| `COLLATION_CORPUS` | `data/sample_corpus.json` | 语料 JSON |

首次启动若索引为空会自动从语料构建；若规则升级导致版本不一致，
服务不自动重建，查询返回 `409 INDEX_VERSION_CONFLICT`，须显式
`POST /admin/rebuild`。

## 接口

- `GET /version` — 索引/内核版本、规则、ICU 版本、条目数、是否待重建
- `GET /entries/sorted?limit=&cursor=` — 稳定全序分页（游标内嵌索引版本）
- `GET /entries/range?lower=&upper=&lower_inclusive=&upper_inclusive=` — 范围检索（边界为排序键）
- `GET /entries/{id}` — 单条（原文 + NFC + 排序键 hex）
- `POST /admin/rebuild` — 重建索引（可带 `{"entries": [...]}`）

每个响应带 `X-Request-ID`；错误体为
`{"error": {category, message, detail, request_id}}`，日志按
`request_id=... step=... key=value` 关联关键步骤与失败原因。

## 测试

```bash
.venv/bin/python -m pytest
```

真实输出（ICU 74.2 / PyICU 2.16.2 / Python 3.12）：

```
42 passed, 1 warning in 0.53s
```

测试要点：

- **对照库逐项比较**：`test_kernel.py` / `test_api.py` 中，参考答案由测试侧
  独立构建的 `icu.Collator`（对照库）与冻结夹具
  `tests/fixtures/reference_orderings.json` 给出，不由被测内核自证。
- **具体场景**：重音（cote/coté/côte/côté）、数字片段（file2 vs file10
  随 numeric 开关反转）、大小写（case_first 两向）、土耳其字符
  （tr 下 `ı < I < i < İ`，与 en 相反）、规范等价（NFC/NFD 同键不同身份）。
- **稳定性**：排序键相等时按载入顺序决胜，重复调用结果确定。
- **索引版本冲突**：规则升级 → 409 INDEX_VERSION_CONFLICT；旧游标 →
  409 STALE_CURSOR；重建后恢复。
- **排序键边界**：`é..z` 在 UTF-8 下是反向区间、排序键下合法 —— 直接
  证明范围边界用的是排序键而非 UTF-8。
