# Rete Production-Rule Engine

一个**结构化、可审查的 Rete 规则匹配网络**实现（Python 3.10+ / FastAPI / SQLite），不是单文件调用壳或固定返回值。

- **规则语言层** `reteapp/lang/`：JSON 规则文档与小型文本 DSL，静态语义编译为匹配计划
- **推理内核层** `reteapp/core/`：Alpha 网络（常量测试 + 字段值索引）、Beta 记忆（变量索引 / WME 索引 / 父子索引）、Join 节点、冲突议程、有界触发引擎
- **证据存储层** `reteapp/storage/`：SQLite 只追加审计（运行头、规则定义、事实增删、激活、触发、结构化判定轨迹）
- **查询接口层** `reteapp/api/`：FastAPI REST，错误带显式失败类别
- **独立测试层** `tests/`：含**不导入被测内核**的全量枚举参考匹配器，逐检查点对比

版本见 `reteapp/version.py`，所有响应头、日志与证据行都带该版本与运行身份。

---

## 1. 干净目录复现

需要 Python ≥ 3.10（开发验证环境：Python 3.12.3）。

```bash
# 1) （可选）虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装固定版本依赖
python3 -m pip install -r requirements.txt

# 3) 运行全部测试，输出结构化复核日志
python3 -m pytest tests/ -v

#    覆盖率报告（要求 >=80%，当前实测见下文“执行结果”）
python3 -m pytest tests/ --cov=reteapp --cov-report=term-missing

# 4) 启动 HTTP 服务
uvicorn reteapp.api.app:app --host 127.0.0.1 --port 8000
```

依赖固定版本（`requirements.txt`，均为本地实际验证版本）：

| 包 | 版本 |
|---|---|
| fastapi | 0.141.1 |
| uvicorn | 0.54.0 |
| pydantic | 2.13.5 |
| httpx | 0.28.1（测试） |
| pytest | 9.1.1（测试） |

`sqlite3` 为 Python 标准库，无需安装。

配置（环境变量，均有默认值；显式参数优先于环境变量）：

| 变量 | 默认 | 含义 |
|---|---|---|
| `RETE_DB_PATH` | `:memory:` | SQLite 证据库路径（持久化示例：`data/evidence.db`） |
| `RETE_MAX_FIRE_ROUNDS` | `100` | `fire_all` 的硬触发上限（无界循环防护） |
| `RETE_AGENDA_MODE` | `manual` | `manual` / `step` / `auto` |
| `RETE_LOG_LEVEL` | `INFO` | 结构化 JSON 日志级别 |

也可以直接运行离线演示（不启动服务）：

```bash
python3 examples/demo.py
```

---

## 2. 请求样例

规则文档（节选，完整夹具见 `tests/fixtures/rules_orders.json`）：

```json
{
  "name": "gold_customer_order",
  "salience": 20,
  "conditions": [
    {"type": "Customer", "constraints": [
      {"field": "id", "variable": "?cid"},
      {"field": "tier", "op": "==", "value": "gold"}]},
    {"type": "Order", "constraints": [
      {"field": "customer", "variable": "?cid"},
      {"field": "amount", "op": ">", "value": 100}]}
  ],
  "action": {"assert": [
    {"type": "Discount", "fields": {"customer": {"variable": "cid"}, "amount": {"value": 10}}}]}
}
```

```bash
BASE=http://127.0.0.1:8000

# 创建运行（编译规则，返回 run_id 作为关联身份）
curl -s -XPOST $BASE/runs -H 'Content-Type: application/json' \
  -d @examples/create_run.json

# 插入事实
curl -s -XPOST $BASE/runs/demo/facts -H 'Content-Type: application/json' \
  -d '{"fact": {"type": "Customer", "fields": {"id": "c1", "tier": "gold", "city": "NYC"}}}'
curl -s -XPOST $BASE/runs/demo/facts -H 'Content-Type: application/json' \
  -d '{"fact": {"type": "Order", "fields": {"customer": "c1", "amount": 250}}}'

# 查看冲突议程（按优先级+稳定键排序，含每个激活的来源事实）
curl -s $BASE/runs/demo/agenda | python3 -m json.tool

# 触发（有界：next / step / all）
curl -s -XPOST $BASE/runs/demo/fire -H 'Content-Type: application/json' \
  -d '{"mode": "all"}' | python3 -m json.tool

# 撤回事实（删除所有依赖该事实的匹配与激活）
curl -s -XDELETE $BASE/runs/demo/facts/2

# 判定轨迹、证据计数、网络索引摘要
curl -s $BASE/runs/demo/trace | python3 -m json.tool
curl -s $BASE/runs/demo/evidence
curl -s $BASE/runs/demo/network | python3 -m json.tool
```

每个响应带 `X-Request-ID`（可用请求头指定）与 `X-Engine-Version`。

---

## 3. 关键语义（验收对照）

### Alpha / Beta 记忆各自有索引

- **AlphaMemory**：`wmes`（成员集合）+ `field_index[field][value] -> set[wme_id]`，右连接探测走索引；常量测试集合相同的 CE **共享**同一 alpha 记忆。
- **BetaMemory**（每条规则每个条件深度一个）：
  - `tokens[key]`：部分匹配（物理 WME 有序元组）
  - `var_index[variable][value] -> set[token_key]`：左连接探测
  - `wme_index[wme_id] -> set[token_key]`：撤回时定位所有依赖 token
  - `children[parent_key]`：向更深 beta 记忆递归级联
- `GET /runs/{id}/network` 可直接查看各记忆与索引规模；测试 `tests/test_network_indexes.py` 断言这些结构确实存在并被使用。

### 撤回删除所有依赖匹配

撤回一个 WME 时：从 alpha 记忆移除 → 经 `wme_index` 找到每个 beta 层的 token → 经 `children` 级联删除更深 token → 终端 token 对应议程项被标记失效。与独立全量参考实现逐检查点对比（`test_retract_then_readd_matches_oracle`）。

### 激活身份、事实组合与重复事实语义

- WME 身份 = 引擎分配的**单调整数 id**，永不复用。
- Token/激活身份 = 有序物理 WME id 元组；议程稳定键 = `规则名[id-id-…]`。
- 变量等值是**值**连接；两条 CE 可由同一物理 WME 满足（CLIPS/Jess 语义），需要互异事实的规则显式写 `id != ?other`。
- 重复内容事实有明确的三策略：`multiset`（默认，两个副本作为不同 WME 共存、各自连接）、`ignore`（保留首个、幂等）、`reject`（422）。

### 冲突议程排序

`salience` 降序 → `sequence`（激活产生顺序）升序 → `(规则名, WME 元组)` 最终稳定键兜底。与输入插入顺序无关地可复现。

### 规则动作不会无界自动运行

- 没有"自动后台循环"：触发只能通过 `fire next | step N | all` 显式发起。
- `fire_all` 受 `max_fire_rounds` 硬上限约束；触达上限返回**显式状态** `status="limit_reached"` 及 `remaining_activations`，绝不伪装成功，也不会无限循环（`test_fire_limit_is_bounded...`）。
- 折射（refraction）：同一存活 token 触发一次后不会再次进议程；token 因撤回被销毁后重建方可再触发。
- 动作支持 `assert`（字段来自常量或绑定变量）、`retract_ce_indices`（撤回匹配到的事实）、`stop`（本轮显式停止，状态 `stopped`）。

### 失败不折叠为成功

错误类别（HTTP 状态）：`fact_validation_error`(422)、`rule_evaluation_error`(422)、`unknown_fact_error`(404)、`unknown_session_error`(404)、`session_error`(400)、`request_validation_error`(422)、未预期错误 500 `internal_error`。未定义的跨类型比较抛 `PredicateError` → 插入**事务性回滚**。

### 日志可关联、显示步骤与判定依据

- 进程日志为单行 JSON（`ts/level/event/run_id/version/...`）。
- 每次测试运行在 `.test-logs/review-<session>.jsonl` 写结构化复核记录：`session_id`（pytest 运行）、`node`（测试/输入身份）、`run_id`、`version`、工作内存快照、参考答案、Rete 答案、diff 与 PASS/FAIL 判定依据。
- SQLite 证据表：`runs / rules / facts / activations / firings / trace`。

---

## 4. 独立参考匹配器（防止"自证"）

`tests/reference_matcher.py` 用 `itertools.product` 对每条规则做**全量笛卡尔枚举**，与被测内核**唯一共享**的是规则数据类和一个纯比较函数——不导入 `reteapp.core` 任何匹配/议程代码。

测试在每个插入/撤回检查点上：参考器对当前工作内存快照重算完整冲突集，与 Rete 终端 beta 记忆直接比较，差集（missing/extra）写入复核日志并断言为空。覆盖：共享条件、多事实连接、撤回后再加、规则互相触发、40 轮随机增删扫掠。

## 5. 本次干净环境执行结果（如实记录）

执行日期：2026-09-28；环境：Linux、Python 3.12.3；新建独立虚拟环境
`python3 -m venv` 后 `pip install -r requirements.txt` 成功。

| 验证项 | 命令 | 结果 |
|---|---|---|
| 全部测试 | `python3 -m pytest tests/` | **62 passed** |
| 覆盖率（分支） | `python3 -m pytest tests/ --cov=reteapp` | **约 91%**（阈值 80%，已写入 `pyproject.toml`） |
| 离线演示 | `python3 examples/demo.py` | `DEMO OK - all oracle checkpoints agreed.`（3 个检查点 expected==actual：7/7、5/5、2/2） |
| HTTP 服务 | `uvicorn reteapp.api.app:app --port …` | `/health`、`/version`、建运行、插事实、议程、触发、撤回、trace、evidence 均实测通过 |
| SQLite 持久化 | `RETE_DB_PATH=data/evidence.db` | 六张表写入运行头/规则/事实/激活/触发/轨迹；关闭后重开可读取（专项测试） |

参考等价性（`tests/reference_matcher.py` 不导入内核）：在 24 个检查点上独立全量枚举与 Rete 终端 beta 记忆逐一对比，全部 `PASS`，覆盖共享条件、多事实连接、撤回后再加、规则链式触发、插入顺序反转、重复事实、40 轮随机增删扫掠。结构化复核日志位于 `.test-logs/review-<session>.jsonl`，每行带 `session_id/node/run_id/version`、工作内存快照、双答案、diff 与判定依据。

有界触发实测：自触发规则在 `max_fire_rounds` 处停止并返回 `status="limit_reached"`、`rounds_used=上限`、`remaining_activations=1`，未出现无限循环。

## 6. 工程结构

```
reteapp/
  config.py              配置层（env -> 不可变 Settings）
  logging_setup.py       单行 JSON 结构化日志（run_id/version 关联）
  lang/                  规则语言：model / parser(JSON+DSL) / predicates / compiler
  core/                  errors / tokens / agenda / network(alpha-beta-join) / engine
  storage/evidence.py    SQLite 只追加证据库
  service.py             运行会话：引擎+证据+关联身份
  api/                   schemas / FastAPI app
tests/
  fixtures/              合成规则夹具（本地、无外部账号）
  reference_matcher.py   独立全量枚举 oracle（静态测试保证不导入内核）
  test_*.py              62 个断言具体结果与失败类别的测试
examples/                请求样例与离线演示
```
