# 受限 Datalog 查询服务

支持**递归正规则**与**分层否定（stratified negation）**的受限 Datalog 后端。
规则在编译期完成变量安全性与否定环检查，推理内核采用**半朴素（semi-naïve）不动点**求值，
每条答案附带可核验的推导证据树；规则版本与事实集版本独立且内容寻址。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`，仅作证据/审计存储，**不**参与递归求值）。

---

## 1. 算法假设与语义

- **域与常量**：整数（`42`）、小写符号（`alice`）、引号字符串（`"Alice"`）三类；变量为大写开头（`X`、`Person`）。
- **事实集语义**：事实必须为基元（ground）。事实集按其规范化文本做 SHA-256 内容哈希（`fact_set_version`）；规则单独哈希（`rule_version`）。同一事实重复出现按集合去重。
- **规则版本**：规则文本规范化（逐条 canonical 后排序）哈希，规则书写顺序与事实增删都不影响 `rule_version`。
- **安全性（编译期，range restriction）**：
  - 规则头变量必须出现在某个**正**体文字中；
  - 否定文字中的变量必须由正体文字约束。
  - 违反则整体拒绝，错误码 `UNSAFE_VARIABLE`，一次性收集全部问题。
- **否定分层**：在“头谓词 → 体谓词”依赖图上求 SCC（迭代 Tarjan）。任一 SCC 内含否定边即拒绝（`NEGATION_CYCLE`，错误详情给出环路径）。通过后按“否定边权重 1、正边权重 0”在 SCC DAG 上求最长路径得到层号；层与层之间各自独立求不动点，否定只引用**严格更低层**的已完成关系（集合差语义）。
- **半朴素不动点**：
  - 每层第 0 轮对所有规则按**轮前快照**全量点火一次；
  - 之后每轮只保留上一轮新元组组成的 delta，每条递归规则按递归正文字位置生成若干“该文字读 delta、其余读主关系”的点火变体，保证每次联结至少含一个新元组，不重复推导；
  - 一轮内所有变体读同一快照、轮末统一吸收，因此推导只依赖“严格更早”的元组；终态确认轮（产出 0）会执行并出现在 trace 中但不计入有效轮数。
- **最小模型**：规则体为合取、无函数符号（finite Herbrand domain），保证不动点有限且唯一（最小 Herbrand 模型）。
- **证据**：每个被推导元组记录“首次产生它的那次点火”（规则 id + 各体文字命中的具体元组与轮次）。首次推导边总是指向更早插入的元组，故证据展开为以事实为叶的 DAG，必然终止。证明树的构建、序列化与叶节点遍历全部为**迭代式**（显式栈，深链不会触发 Python 递归上限）；另设默认 10000 的深度安全阀，触发时显式标记 `depth_truncated`（在响应 `uncertainty` 中单列），而非伪造或截断得无声无息。
- **查询**：对已物化结果做匹配（含常量化、重复变量合一），不触发外部数据库递归查询。

---

## 2. 模块关系

```
datalog_service/
├── language/                 # 规则语言层
│   ├── ast.py                #   不可变 AST：Variable/Constant/Atom/Literal/Rule/Program
│   ├── parser.py             #   词法 + 递归下降解析（程序与查询目标）
│   ├── compiler.py           #   编译期：元数、变量安全、SCC 分层、版本哈希
│   └── errors.py             #   稳定错误码：PARSE/COMPILE/QUERY/STATE
├── engine/                   # 推理内核
│   ├── relational.py         #   项与具体元组的合一匹配
│   ├── fixpoint.py           #   半朴素不动点（delta 变体、轮次/trace、物化结果）
│   └── derivations.py        #   推导记录与可展开证明树
├── storage/
│   └── evidence_store.py     # SQLite：程序快照/物化/元组证据/请求审计（内容寻址去重）
├── query/
│   └── answering.py          # 对物化关系匹配查询目标、挂载证明树
├── api/
│   ├── schemas.py            # Pydantic 请求/响应模型
│   ├── app.py                # 工厂：中间件（请求 ID/限流）、异常处理、装配
│   └── routes.py             # 提交、查询、证据浏览、请求日志等端点
├── service.py                # 编排：解析→编译→求值→持久化（进程内缓存）
└── config.py                 # 环境变量配置（无密钥）

tests/
├── naive_oracle.py           # 独立朴素求值预言机（全量重算，自有联结/绑定实现）
├── test_parser.py / test_ast.py
├── test_compiler.py          #   安全变量、元数、否定环、分层断言
├── test_engine.py            #   祖先闭包手算断言、半朴素 vs 朴素、规则顺序无关
├── test_proofs.py            #   证明树与防御分支
├── test_query.py / test_storage.py / test_api.py
fixtures/                      # 合成夹具：祖先、递归排除、可达性
```

SQLite 不替代内核：它只持久化“程序版本、事实版本、推导元组及依据、请求审计”。

---

## 3. 依赖版本

| 依赖 | 版本 | 用途 |
|---|---|---|
| Python | 3.12 | 运行时 |
| fastapi | 0.141.1 | HTTP 接口 |
| pydantic | 2.13.5 | 请求/响应模型 |
| starlette | 1.7.0 | FastAPI 底层 |
| uvicorn | 0.54.0 | ASGI 服务 |
| httpx | 0.28.1 | 测试客户端传输 |
| pytest | 9.1.1 | 测试 |
| pytest-cov | 7.1.0 | 覆盖率 |

SQLite 使用 Python 标准库自带的 `sqlite3`。

---

## 4. 本地验证

```bash
# 1) 准备环境
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# 2) 全部测试 + 覆盖率（预期：94 passed，整体覆盖率 98%，核心模块 94%+）
python -m pytest --cov=datalog_service --cov-report=term-missing

# 仅单元测试 / 仅集成测试
python -m pytest -m unit
python -m pytest -m integration

# 3) 启动服务（默认 SQLite 落在 ./data/evidence.db）
DATALOG_DB_PATH=/tmp/dl_demo.db \
  python -m uvicorn datalog_service.api.app:create_app --factory --port 8000
```

### 4.1 冒烟命令与预期判断

```bash
# 健康检查 → {"status":"ok",...}
curl -s http://127.0.0.1:8000/healthz

# 提交祖先程序（-H 指定请求身份，响应头与审计日志都会带回同一 ID）
curl -s -X POST http://127.0.0.1:8000/programs \
  -H "Content-Type: application/json" -H "X-Request-ID: demo-1" \
  -d "{\"program\": $(python -c 'import json;print(json.dumps(open("fixtures/ancestor.dl").read()))')}"
# 预期：status=ok，rule_count=2，fact_count=6，strata=[["r1","r2"]]，
#       返回 rule_version / fact_set_version / program_id / materialization_id

PID=<上一步的 program_id>

# 查询 alice 的全部祖先 → answer_count=6
curl -s -X POST "http://127.0.0.1:8000/programs/$PID/query" \
  -H "Content-Type: application/json" \
  -d '{"query":"ancestor(alice, Y)?","include_proofs":true}'
# 预期 Y ∈ {bob, frank, carol, grace, dave, erin}；每个答案带 proof，
#       证明树叶节点全部为 parent 事实；erin 的证明链含 4 条事实。

# 请求身份关联日志 → 同一 request_id 可回溯端点、目标、结果数、版本
curl -s http://127.0.0.1:8000/requests/demo-1

# 不安全变量 → HTTP 422，error_code=COMPILE_ERROR，failures[].code=UNSAFE_VARIABLE
curl -s -X POST http://127.0.0.1:8000/programs -H "Content-Type: application/json" \
  -d '{"program":"q(a). p(X,Y) :- q(X)."}'

# 否定依赖环 → HTTP 422，code=NEGATION_CYCLE，location.cycle 给出环
curl -s -X POST http://127.0.0.1:8000/programs -H "Content-Type: application/json" \
  -d '{"program":"e(a). p(X) :- e(X), not q(X). q(X) :- e(X), not p(X)."}'
```

### 4.2 关键测试的判断口径

- **祖先关系**：`tests/test_engine.py::test_ancestor_closure_matches_hand_computed_transitive_closure`
  用手工枚举的 13 对（6 条 parent + 长度 2/3/4 的 7 条）断言，不依赖被测内核自证。
- **递归排除**：`indirect = ancestor − parent`，断言 7 对且 `(alice,bob)∉indirect`、`(alice,erin)∈indirect`；另有 `root` 分层否定结果恰为 `{alice}`。
- **不安全变量**：分别断言头变量未绑定、否定文字变量未绑定两类，检查错误码与 `location.position`。
- **半朴素 vs 朴素**：`test_seminaive_matches_independent_naive_oracle_on_all_fixtures`
  用 `tests/naive_oracle.py`（独立实现的朴素全量重算）对三个夹具逐谓词比对闭包。
- **规则顺序无关**：对同一程序的多种事实/规则排列求闭包与版本，断言完全一致。
- **证据**：证明树叶节点必须全部为事实；证明缺失标记 `unexplained`、深度截断标记 `depth_truncated`，并在查询响应的 `uncertainty` 中单列。

### 4.3 HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| POST | `/programs` | 提交规则+事实，编译并物化，返回版本、分层、逐轮 trace |
| POST | `/programs/{id}/query` | 查询目标，返回绑定与（可选）证明树 |
| GET | `/programs/{id}` | 程序快照：规范化文本、规则、层、trace |
| GET | `/programs/{id}/tuples/{predicate}` | 某谓词全部元组及来源（fact/derived、规则、轮次） |
| GET | `/requests/{request_id}` | 按请求身份回溯审计记录 |

错误分类：解析错误 `PARSE_ERROR`(400)、编译错误 `COMPILE_ERROR`(422，含
`ARITY_MISMATCH`/`UNSAFE_VARIABLE`/`NEGATION_CYCLE`)、查询错误 `QUERY_ERROR`(400)、
未知 id `STATE_ERROR`(404)、客户端限流 `RATE_LIMITED`(429，默认 120 次/分钟/IP，
可用 `DATALOG_RATE_LIMIT_PER_MIN=0` 关闭)、未预期错误 `INTERNAL_ERROR`(500)。
失败原因统一放在 `failures`，不确定结论（证明截断等）放在 `uncertainty`。

---

## 5. 测试状态

- 全部 **94** 个测试通过（**73** 单元 + **21** 集成），整体覆盖率 **98%**（推理内核 `fixpoint` 100%，`language` 94–99%，`storage` 99%）。
- 未使用或被跳过的测试：无。
- 阻塞式 SQLite 访问的端点使用同步 `def`（由 Starlette 线程池执行），中间件只做非阻塞的请求 ID 与内存限流。
- 含 3000 层推导链的迭代安全测试（`test_deep_chain_proof_is_iteration_safe`），验证证明构建/序列化/遍历均不触发递归上限。
