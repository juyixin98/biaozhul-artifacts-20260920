# fspm-service — 事件序列频繁顺序模式挖掘（最大间隔 + 最小支持）

纯后端服务：Python + FastAPI + SQLite。输入为事件序列语料，输出为满足
最大间隔约束的频繁顺序模式及其嵌入证据。所有数据均为本地合成夹具
（`fixtures/`），无任何外部账号或真实业务数据依赖。

## 算法假设（语义约定）

- **序列**：有序事件列表，事件 = (符号, 可选时间戳)。若带时间戳，同一序列内
  时间戳必须非递减（语料校验强制）；全语料要么都有时间戳要么都没有。
- **嵌入（embedding）**：模式 `p = (s1..sk)` 在序列中的一个嵌入是严格递增的
  位置元组 `(i1<...<ik)`，符号逐一匹配，且相邻匹配位置满足两类间隔约束。
- **位置间隔与时间间隔分别声明**：`max_pos_gap` 限制相邻匹配的下标差
  （`i_{j+1} - i_j <= max_pos_gap`，含边界），`max_time_gap` 限制相邻匹配的
  时间戳差（含边界）。两者均可独立设为 `null`（不限制）；同时设置时同时生效。
  `max_time_gap` 要求所有事件带时间戳，否则拒绝（`constraint_error`）。
- **支持度按序列身份计数**：一个序列无论含多少个嵌入，对模式支持度只贡献 1。
  重复符号的多次匹配不会多计。
- **投影不剪替代嵌入**：内核以前缀投影方式增长，投影库为每个序列保留该前缀的
  **全部**嵌入（而非第一个/最短一个）。扩展时从每个保留嵌入出发，因此"前一个
  嵌入在间隔窗口内无法扩展、后一个可以"的情形不会被错误剪掉
  （见 `fixtures/alternative_embeddings.json`）。
- **输出嵌入证据**：每个频繁模式附带每个序列中的匹配位置列表
  （每序列上限 `FSPM_MAX_EMBEDDINGS_PER_SEQUENCE`，默认 64，截断时
  `evidence_complete=false`）。

## 模块关系

```
app/
  config.py            配置层：db 路径、模式长度上限、证据上限、日志级别（环境变量覆盖）
  logging_setup.py     JSON 结构化日志：run_id / step / support / min_support / decision
  models/domain.py     领域模型：Event, Sequence, GapConstraints, PatternResult, EmbeddingEvidence
  corpus/
    schema.py          语料规范：pydantic 边界校验（唯一序列 id、时间戳非递减、时间戳一致性）
    store.py           SQLite 持久化：corpora / events(符号索引) / runs / patterns
    fixtures.py        合成夹具加载器（fixtures/*.json）
  mining/
    constraints.py     间隔约束检查与约束-语料一致性校验
    embedding.py       单模式嵌入枚举（回溯，间隔单调可安全截枝）
    kernel.py          前缀投影增长挖掘器（支持度按序列计数，逐模式记录判定日志）
  api/
    validation.py      挖掘请求查询校验（min_support>=1, gap 边界等）
    routes.py          路由：/corpora, /mine, /runs/{id}, /runs/{id}/patterns, /health, /meta
    main.py            应用工厂与错误映射（错误一律返回非 2xx + 错误类别）
tests/                 独立测试层（含 tests/naive_miner.py 独立朴素实现做差分对照）
scripts/mine_fixture.py  本地验证 CLI：内核输出对照手工参考答案
fixtures/              合成语料 + 手工计算的参考结果（非内核生成）
```

数据流：`POST /corpora`（校验→SQLite）→ `POST /mine`（加载序列→内核挖掘→
模式与证据入库→返回 run_id 与结果）→ `GET /runs/{run_id}[/patterns]` 复核。

## 依赖版本

- Python 3.12.3
- fastapi 0.141.1 / uvicorn 0.54.0 / pydantic 2.13.5（`requirements.txt`）
- 测试：pytest 9.1.1 / pytest-cov 7.1.0 / httpx 0.28.1（`requirements-dev.txt`）
- SQLite 为 Python 标准库 `sqlite3`（运行时版本见 `GET /meta`）

## 本地验证命令与预期判断

```bash
pip install -r requirements-dev.txt

# 1) 单元/参考/差分/API 测试（43 项）
python3 -m pytest -q
# 预期：43 passed。覆盖：python3 -m pytest --cov=app -q（当前 96%）

# 2) 夹具对照（内核输出 vs 手工参考答案，9 个用例）
python3 scripts/mine_fixture.py --quiet
# 预期：9/9 cases passed；任一不符即 FAIL 并以非零码退出

# 3) 启动服务并手工验证
python3 -m uvicorn app.api.main:app --port 8931
curl localhost:8931/health                       # {"status":"ok"}
curl localhost:8931/meta                         # 组件版本
curl -X POST localhost:8931/corpora -H 'Content-Type: application/json' \
  -d '{"name":"demo","sequences":[{"sequence_id":"S1","events":[{"symbol":"A"},{"symbol":"A"},{"symbol":"B"}]},{"sequence_id":"S2","events":[{"symbol":"A"},{"symbol":"B"}]}]}'
curl -X POST localhost:8931/mine -H 'Content-Type: application/json' \
  -d '{"corpus_id":"<上一步返回的 corpus_id>","min_support":2}'
# 预期：A 支持度 2（S1 中两个 A 只计一次）、B 支持度 2、A>B 支持度 2，
# 且 A>B 的 embeddings 含 S1 的 [0,2] 与 [1,2]、S2 的 [0,1]
```

判定依据可复核：每次挖掘的日志为 JSON 行，携带 `run_id`、`step`
（`run_created`/`pattern_eval`/`run_completed`/`run_failed`）、每个模式的
`support` 与 `min_support` 及 `decision`(keep/prune)；`GET /runs/{run_id}`
返回运行状态、参数与组件版本。失败运行落库为 `FAILED` 并带错误类别，
不会以成功返回。

## 测试设计说明

- `tests/test_kernel_reference.py`：内核输出对照 `fixtures/` 中**手工推导**的
  支持度与嵌入证据（重复事件、位置间隔边界、时间间隔边界、时间并列、多嵌入、
  替代嵌入保留），并断言应缺席的模式确实缺席（模式集合精确相等）。
- `tests/test_kernel_differential.py`：内核 vs `tests/naive_miner.py`（基于
  itertools 组合的独立朴素实现）在种子化随机语料上差分一致。
- `tests/test_embedding.py`：嵌入枚举的边界单元测试（含间隔含边界、零时间间隔、
  无时间戳配时间约束的报错类别）。
- `tests/test_api.py`：端到端结果断言 + 失败类别（`validation_error` /
  `constraint_error` / `not_found`）+ 失败运行落库为 FAILED + 日志携带
  run_id 与 keep/prune 判定。

## 测试状态

最近一次本地运行（2026-09-28）：43/43 通过，覆盖率 96%，夹具对照 9/9 通过。
无未通过、无跳过、无未运行的测试。
