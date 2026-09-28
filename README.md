# 事件序列频繁顺序模式挖掘后端 (Sequential Pattern Mining API)

纯后端服务，从事件序列中挖掘**频繁顺序模式**，支持**最大位置间隔**、**最大时间间隔**
和**最小支持度**。技术栈：Python + FastAPI + SQLite，全部数据来自本地合成夹具，
无需任何生产账号或外部依赖。

---

## 1. 算法假设（语义约定）

| 主题 | 约定 |
|---|---|
| 模式 | 符号序列 `P = (s1, s2, …, sk)`，要求在目标序列中按**严格递增位置**出现 |
| 嵌入 (embedding) | 一组严格递增位置 `(p1 < p2 < … < pk)`，使 `seq[pi].symbol == si` |
| **支持度** | 拥有 ≥1 个合法嵌入的**不同序列身份 (sequence id) 数量**。同一序列中无论匹配多少次（重复符号、多个嵌入）都只计 **1** |
| 出现次数 `occurrences` | 嵌入总数，单独报告，**不参与支持度** |
| 位置间隔 | `p - q`（下标距离）。相邻事件间隔为 **1**；边界**含等号**（`<= max_gap_position`） |
| 时间间隔 | `timestamp(p) - timestamp(q)`。时间戳相等间隔为 **0**，`max_gap_time=0` 合法（时间并列）；边界含等号 |
| 双间隔 | 位置间隔与时间间隔**分别声明、相互独立**；同时给出时一对事件必须**同时**满足两者（交集，不是取其一） |
| 时间戳 | 单条序列内必须全有或全无，且**非递减**（相等值表示并列）。声明 `max_gap_time` 时要求所有事件都带时间戳 |
| `min_support` | `int >= 1` 为绝对序列数；`float in (0,1]` 为比例，按 `ceil(比例 × 序列数)` 向上取整 |
| 位置来源 | 事件 `position` 一律取其在序列中的序号（0 基），**不接受客户端传入** |

### 投影数据库为何不会错误剪掉替代嵌入

采用 PrefixSpan 风格的投影增长。某个前缀在一条序列上的投影保存的是
**所有**能接在“某个前缀嵌入”之后的后缀起点集合，而不是只保留第一个匹配：

- 种子投影：`{ j | seq[j].symbol == x }`（每个出现位置都保留）；
- 扩展投影：`{ j | seq[j].symbol == y 且 ∃ q ∈ 旧后缀集, q < j 且 pair_ok(q,j) }`。

因此即使某个后缀起点对当前模式“非必需”，也会被保留——它可能是后续符号在间隔
约束下唯一可达的前驱。`pruning_trap` 夹具专门验证这一点：
序列 `A C A B` 在 `max_gap_position=1` 下，模式 `<A,B>` 的唯一嵌入是 **(2,3)**，
早期的 A(0) 到不了 B(3)；贪婪“首个匹配即停”的实现会错误地报告该模式不存在。

### 嵌入证据

支持度记账（投影引擎，`miner/engine.py`）与嵌入枚举（证据枚举器，
`miner/evidence.py`）是**两套独立实现**，只共享同一个“合法事件对”谓词
`ValidatedConstraints.pair_ok`。门面（`miner/facade.py`）对二者做**双向一致性
校验**：引擎声称支持的序列必须能枚举出嵌入，枚举器发现的支持集合也必须与引擎
完全一致，否则抛出 `mining_internal_error`（500），**绝不返回看似成功的错误结果**。

---

## 2. 模块关系（分层工程）

```
app/
├── config.py          配置层：环境变量(SPM_*)可覆盖，不可变 dataclass
├── errors.py          错误分类法：稳定 error code + HTTP 状态映射
├── observability.py   运行级结构化日志：run_id 贯穿、JSONL 文件、版本/步骤/判定
├── models.py          索引与模型层：pydantic 请求/响应/领域模型(冻结)
├── corpus/            语料规范层
│   ├── spec.py        规范化与校验（位置序号、时间戳全有或全无、非递减…）
│   └── fixtures.py    本地合成夹具（重复符号/时间并列/多嵌入/间隔边界/剪枝陷阱）
├── storage/
│   └── repository.py  SQLite 仓储：corpora/sequences/events 三表 + 符号索引
├── miner/             挖掘内核
│   ├── constraints.py 查询验证 + 统一的位置/时间间隔谓词
│   ├── engine.py      投影数据库增长引擎（决定频繁模式与支持身份）
│   ├── evidence.py    独立的全嵌入枚举器（输出证据）
│   └── facade.py      编排引擎+证据，双向一致性校验，组装响应
├── service.py         应用服务层：编排存储/规范/挖掘，无 HTTP 类型泄漏
└── api.py             FastAPI 薄层：路由 + 显式错误处理（create_app 工厂）

tests/
├── conftest.py                     隔离 DB/日志目录、内核调用助手
├── oracle.py                       ★独立暴力 Oracle（不 import app.miner）
├── test_oracle_equivalence.py      引擎 vs Oracle：手工用例 + 固定种子穷举 fuzz
├── test_fixtures_expectations.py   人工推算的夹具具体期望值（带推导注释）
├── test_support_semantics.py       支持度按序列身份、重复不多计
├── test_projection.py              投影保留替代嵌入、剪枝陷阱、多嵌入
├── test_gap_constraints.py         位置/时间间隔边界、并列、双约束交集
├── test_query_validation.py        查询约束边界与失败类别
├── test_corpus_spec.py             语料规范失败类别
├── test_api.py                     端到端 HTTP（含 500 安全网）
├── test_storage.py                 SQLite 持久化/级联/计数
├── test_logging.py                 日志与 run_id/输入/版本/判定关联
├── test_safety_nets.py             引擎/枚举器不一致 → 内部错误
└── test_config.py                  配置覆盖与冻结
```

依赖方向单向向内：`api → service → {storage, corpus, miner} → models/errors`。
HTTP 类型不进入 service/miner，因此内核可脱离 Web 直接测试。

---

## 3. 依赖版本

开发与验证环境（`requirements.txt` 锁定）：

| 包 | 版本 | 用途 |
|---|---|---|
| Python | 3.12.3 | 运行时（标准库 `sqlite3` = SQLite 3.45.1） |
| fastapi | 0.141.1 | HTTP 层 |
| pydantic | 2.13.5 | 模型与严格数值类型（随 fastapi 安装） |
| uvicorn | 0.54.0 | ASGI 服务器 |
| httpx | 0.28.1 | TestClient / 冒烟脚本 |
| pytest | 9.1.1 | 测试框架 |

安装：`pip install -r requirements.txt`

---

## 4. 本地验证命令与预期判断方式

### 4.1 单元/集成测试（推荐先跑）

```bash
python3 -m pytest -q
```

**预期**：`100 passed`，退出码 0。未通过会以非零退出码和具体断言失败呈现。

带覆盖率（应用代码目标 ≥ 80%，当前为 **100%**）：

```bash
python3 -m coverage run -m pytest -q && python3 -m coverage report --include="app/*"
```

### 4.2 真实 HTTP 端到端冒烟

```bash
bash scripts/smoke.sh
```

脚本会启动真实 uvicorn（临时端口 8099），用 curl 校验：剪枝陷阱的后期嵌入、
时间并列 gap=0、显式错误码（404/422）、以及日志与 run_id/版本/步骤的关联。
**预期**最后一行 `ALL SMOKE ASSERTIONS PASSED`，退出码 0。

### 4.3 手动启动并调用

```bash
python3 -m uvicorn app.api:app --port 8077
# 另开终端：
curl -s http://127.0.0.1:8077/health
curl -s -X POST http://127.0.0.1:8077/v1/fixtures/pruning_trap            # 建夹具
curl -s http://127.0.0.1:8077/v1/corpora                                  # 取 corpus_id
curl -s -X POST http://127.0.0.1:8077/v1/mine -H 'Content-Type: application/json' \
  -d '{"corpus_id":"<ID>","min_support":2,"max_gap_position":1}'
```

交互式文档：启动后访问 `http://127.0.0.1:8077/docs`。
每次挖掘的运行日志在 `logs/<run_id>.jsonl`（响应中的 `run_id` 即文件名）。

### 4.4 主要 HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查与版本 |
| GET | `/v1/fixtures` | 列出合成夹具 |
| POST | `/v1/fixtures/{name}` | 载入指定夹具为新语料（201） |
| POST | `/v1/corpora` | 创建语料（201） |
| GET | `/v1/corpora` / `/v1/corpora/{id}` | 列出 / 查看语料 |
| DELETE | `/v1/corpora/{id}` | 删除（显式级联，204） |
| POST | `/v1/mine` | 挖掘，返回模式、支持身份集合与全部嵌入证据 |

### 4.5 失败如何标记（不会被统一返回成功）

| 场景 | HTTP | `error` code |
|---|---|---|
| 语料不存在 | 404 | `not_found` |
| 语料为空 / 序列为空 / 重复序列 id / 时间戳问题 | 422 | `empty_corpus` / `invalid_sequence` / `invalid_event` |
| min_support、间隔、模式长度非法；时间间隔但无时间戳 | 422 | `invalid_constraint` |
| 请求体结构/严格类型错误 | 422 | `invalid_request_body` |
| 语料 id 冲突 | 409 | `corpus_conflict` |
| 引擎/证据内部不一致或其它未预期异常 | 500 | `mining_internal_error` / `internal_error`（带 run_id） |

---

## 5. 测试答案的独立性

- **独立暴力 Oracle**（`tests/oracle.py`）不导入任何 `app.miner` 代码：它枚举序列
  的全部严格递增位置组合，按声明的间隔逐一判定，再以不同序列身份计数。
- `test_oracle_equivalence.py` 既比对**人工构造**用例，也用**固定种子**
  （`random.Random(20260927)`，可复现）生成 60 组小语料 × 约束组合，逐一比对
  频繁模式集合、支持身份集合和**每一个嵌入位置**。
- `test_fixtures_expectations.py` 的期望值为人工推算并附推导注释，锁定夹具契约。
- 因此参考答案**不是**由被测核心实现自身生成的。
