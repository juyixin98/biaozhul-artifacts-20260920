# STRIPS 离线规划服务

一套基于 **Python 3.11+ / FastAPI / SQLite** 的 STRIPS 式离线规划后端。支持
**正/负前置条件**与**添加/删除效果**，提供有界搜索（广度优先 BFS、一致代价
UCS、A\*、贪心最佳优先），并且**返回的每个动作序列都由独立执行器从初始状态
逐步重放验证**。所有运行结果（成功、无解、界限未知、输入错误、状态冲突）都
带稳定的 `run_id` 持久化到 SQLite 与 JSONL 重放日志。

全部数据均为本地合成夹具，无任何生产账号或真实业务数据。

---

## 1. 固定语义（验收边界）

| 边界 | 规则 |
|------|------|
| 前置条件 | 正条件要求每个原子在当前状态中**存在**；负条件要求每个原子**缺席** |
| 效果判定 | add 与 del 都从**同一个前驱状态**判定 |
| 增删冲突 | 同一提升字面量在某动作的 add 与 del 中同时出现 → 校验拒绝（`ADD_DELETE_CONFLICT`，对所有绑定自相矛盾）；不同参数因绑定别名（`?from == ?to`）落地为同一基原子时不拒绝，应用顺序固定为**先删后加、add 必胜**，结果不依赖集合迭代顺序 |
| 状态去重 | 状态 = 基原子的 `frozenset`；`StateEncoder` 按固定原子序生成整数位掩码，结构相等的状态（含环路回到的状态）共享同一身份 |
| 最优性承诺 | BFS：动作数最少（忽略代价）；UCS：代价和最小；A\*：仅在启发式**可采纳**时代价最优（`hmax` 可采纳，`goalcount` 不可采纳，A\* 直接拒绝它）；贪心：`optimal=false`，无任何最优承诺 |
| 搜不完 | 触达 `max_expanded` / `max_depth` / `max_seconds` 任一界限 → `status=LIMIT`、`category=RESOURCE_LIMIT`、`solvability=UNKNOWN`，并回传界限名与值，**绝不把猜测包装成计划** |
| 无解 | 可达状态空间被穷举仍未达目标 → `status=UNSOLVABLE`（可证明无解） |
| 独立执行器 | 规划器与执行器不共享中间状态；执行器只拿到问题与动作标签列表，自行从 `init` 逐步检查正负前置条件、应用转移、核对目标。计划与执行代价不一致也会被判为 `COMPUTATION_FAILED` |

动作代价必须为**有限正数**（解析期校验），这是最优抽取与正代价重开安全性的前提。

---

## 2. 工程结构（模块边界，非空壳）

```
src/strips_planner/
├── language/                 # 规则语言
│   ├── parser.py             #   JSON/S-表达式形状解析（语法错误）
│   └── validator.py          #   语义校验 + 类型化落地（grounding）
├── core/                     # 规划内核
│   ├── state.py              #   状态编码、去重、STRIPS 转移
│   ├── executor.py           #   独立执行器（逐步证据 + 失败分类）
│   ├── heuristics.py         #   zero / hmax(可采纳) / goalcount(不可采纳)
│   └── search.py             #   BFS / UCS / A* / 贪心 + 界限语义
├── storage/
│   ├── database.py           #   SQLite 证据库（每次运行一行）
│   └── run_log.py            #   追加式 JSONL 重放日志
├── service/planning.py       # 编排：解析→校验→搜索→独立执行→持久化
├── api/app.py                # FastAPI HTTP 边界与错误码映射
└── main.py                   # ASGI 入口
fixtures/                     # 最小合成夹具（3 个域）
tests/
├── oracle.py                 # ★ 独立参考实现（自写 BFS/Dijkstra/反向 h*），
│                             #   不 import 被测搜索内核；期望答案由它产生
└── test_*.py                 # 89 个测试，断言具体结果与失败类别
examples/                     # curl 与 Python 调用示例
scripts/                      # 启动与一键复现脚本
results/<check-时间戳>/       # 一次真实运行的可复核产物（已随仓库保留一份）
```

模块间数据契约：`parser.RawProblem → validator.Problem（含 GroundAction）→
search.SearchOutcome → executor.ExecutionTrace → storage.RunRecord`；错误契约
集中在 `errors.py`，任何层都不抛出未分类异常穿越 HTTP 边界。

---

## 3. 快速开始

需要 Python 3.11+（在 3.12.3 验证）。

```bash
python3 -m pip install -r requirements.lock   # 精确锁定，见 §7
bash scripts/run_service.sh                   # http://127.0.0.1:8000
# 另一个终端：
bash examples/curl_examples.sh                # 10 个正常/异常调用场景
# 或：
PYTHONPATH=src python3 examples/python_client.py --algorithm bfs
```

交互式 API 文档：启动后访问 `http://127.0.0.1:8000/docs`。

### 请求体形状

```json
{
  "problem": {
    "name": "resource-ops",
    "types": ["location"],
    "objects": {"location": ["depot", "site1"]},
    "predicates": {"at": {"types": ["location"], "static": false}},
    "actions": [{
      "name": "go", "cost": 1,
      "parameters": [{"name": "?x", "type": "location"},
                     {"name": "?y", "type": "location"}],
      "preconditions": {"pos": ["(at ?x)"], "neg": []},
      "effects": {"add": ["(at ?y)"], "del": ["(at ?x)"]}
    }],
    "init": ["(at depot)"],
    "goal": {"pos": ["(at site1)"], "neg": []}
  },
  "options": {"algorithm": "astar", "heuristic": "hmax",
              "max_expanded": 100000, "max_depth": 1000, "max_seconds": 30}
}
```

原子既支持 S-表达式字符串 `"(at r1 site1)"`，也支持列表 `["at","r1","site1"]`。

---

## 4. HTTP 接口与错误分类

| 方法/路径 | 用途 |
|-----------|------|
| `POST /api/v1/plans` | 解析、校验、搜索并独立验证，返回计划 |
| `POST /api/v1/plans/execute` | 对给定标签列表做独立重放（可带 `problem` 或引用已有 `run_id`） |
| `GET /api/v1/runs?status=&category=&limit=` | 列出证据行 |
| `GET /api/v1/runs/{run_id}` | 取一行完整证据 + `replay_log` 时间线 |
| `GET /healthz` | 存活检查 |

统一响应包：`{"run_id", "success", "result", "error"}`。可区分的失败类别：

| category | HTTP | code 示例 | 含义 |
|----------|------|-----------|------|
| `INPUT_INVALID` | 400 | `SYNTAX_ERROR` / `NOT_A_LIST` / `INVALID_LIMIT` | 请求形状/JSON/取值错误 |
| `INVALID_PROBLEM` | 422 | `UNKNOWN_REFERENCE` / `TYPE_MISMATCH` / `ADD_DELETE_CONFLICT` / `STATIC_PREDICATE_MODIFIED` | 形状合法但语义不合法，回带 issue 列表 |
| `STATE_CONFLICT` | 409 | `MISSING_PRECONDITION` / `NEGATIVE_PRECONDITION_VIOLATED` / `UNKNOWN_ACTION` / `GOAL_NOT_REACHED` | 执行器在某一步发现状态冲突（含失败步与冲突文字） |
| `RESOURCE_LIMIT` | 200 | `SEARCH_LIMIT_REACHED` | 触界；`details.solvability="UNKNOWN"` |
| `NOT_FOUND` | 404 | `RUN_NOT_FOUND` | 未知 `run_id` |
| `COMPUTATION_FAILED` | 500 | `NON_ADMISSIBLE_HEURISTIC` / `COST_MISMATCH` / `PLAN_NOT_VALIDATED` | 内核不变量被违反或规划器与执行器不一致 |

---

## 5. 验收场景与已核实结果

合成夹具 `fixtures/resource_ops.json` 刻意构造了**动作数最优 ≠ 代价最优**、
**循环动作**与**无解目标**：

- 目标：机器人在 `site2` 且箱子 `crate_a` 存放在 `site2`；`site1` 初始被封锁。
- 直达快车 `depot→site2` 代价 **9**；携带箱子走快车 = pick(1)+express(9)+drop(1)
  = **代价 11 / 3 动作**（动作数最少但更贵）。
- 廉价走廊：先 `clear_block`(5) 再 pick(1)+两步移动(2)+drop(1)
  = **代价 9 / 5 动作**（更便宜但更长）。

实测（随仓库保留在 `results/check-20260927T193646Z/`）：

| 场景 | 状态 | 具体断言结果 |
|------|------|--------------|
| BFS | FOUND | 3 动作、代价 11、`optimal=true`（最少动作数） |
| UCS / A\*(hmax) | FOUND | 5 动作、代价 **9**、`optimal=true`（最低代价） |
| 贪心(goalcount) | FOUND | 计划可执行，`optimal=false`（无承诺） |
| 无解夹具（孤岛 `vault`） | UNSOLVABLE | 穷举可达空间后证明不可达，无计划 |
| `max_depth=1` | LIMIT | `RESOURCE_LIMIT`，`solvability=UNKNOWN`，回传界限 |
| 移动进入封锁点 `/execute` | 409 | `NEGATIVE_PRECONDITION_VIOLATED`，失败步 0，冲突文字 `-(blocked site1)` |
| 坏 JSON / 未知对象 | 400 / 422 | `INPUT_INVALID:SYNTAX_ERROR` / `INVALID_PROBLEM:UNKNOWN_REFERENCE` |

### 测试如何防止"用被测核心给自己出答案"

- `tests/oracle.py` **独立重写**了转移语义、BFS（最短动作数）、Dijkstra（最低
  代价）、可达集枚举与**反向 Dijkstra 求真实 h\***，只与被测代码共享数据类。
- `test_search.py` / `test_cross_engine.py` 用 oracle 的 `min_length`、`min_cost`
  断言 BFS/UCS/A\* 的具体计划长度、代价与动作多重集合，并跨夹具穷举比对。
- `test_heuristics.py` 在**每个可达状态**上断言 hmax ≤ 真实 h\*（可采纳性穷举
  验证）、目标态 h=0、初态严格 0 < hmax < h\*（信息性）。
- 循环/自环用例断言扩展数不超过 oracle 的可达状态数（去重有效），对角别名
  （`?from==?to`）断言固定 add-wins 结果。
- 异常测试断言**具体 category/code/失败步/冲突文字**，而非"接口能调用"。

---

## 6. 测试与可复核产物

```bash
python3 -m pytest                                  # 89 passed
python3 -m pytest --cov=strips_planner --cov-report=term-missing   # 行覆盖 91%
bash scripts/reproduce.sh                         # 一键：装锁→测试+覆盖→真实起服务跑 10 场景→落证据
```

`scripts/reproduce.sh` 每次生成带运行编号的目录 `results/check-<UTC时间戳>/`：

- `manifest.txt`   — 运行编号与 Python 版本
- `tests.log`      - 带覆盖率的详细测试输出
- `server.log`     - 真实 uvicorn 启动日志
- `curl_examples.log` — 10 个 HTTP 场景的原始响应
- `evidence.db.saved` — 自包含 SQLite 证据库（停机检查点后复制，无 WAL 侧车文件）
- `runs.jsonl.saved`  — 每运行 `RECEIVED→PARSED→VALIDATED→SEARCH→EXECUTED→
                        COMPLETED/FAILED` 时间线，含展开/生成计数、触界、失败步、
                        判断理由，可凭 `run_id` 重放问题

用只读 SQL 即可审计，例如：

```bash
python3 - <<'PY'
import sqlite3
c = sqlite3.connect("results/check-20260927T193646Z/evidence.db.saved")
for r in c.execute("SELECT run_id, status, category, path_cost FROM runs ORDER BY created_at"):
    print(r)
PY
```

---

## 7. 依赖锁定

- `requirements.txt`  — 宽松上下界，描述直接依赖。
- `requirements.lock` — 已验证环境（CPython 3.12.3 / Linux）的**精确版本**，
  含全部传递依赖；最小安装，不含 uvloop/watchfiles/brotli 等可选加速器。

运行期仅依赖 `fastapi / uvicorn / pydantic`（及其传递包）；测试额外需要
`pytest / httpx / pytest-cov`。无外部网络服务、无密钥、无真实账号。

---

## 8. 设计取舍备注

- 规划在请求内同步执行，默认界限 `100_000` 展开 / 深度 `1_000` / 30 秒，服务端
  硬上限分别为 `1_000_000` / `10_000` / 60 秒；离线批量场景可调大，触界语义不变。
- SQLite 单连接 + WAL，足以承载本地合成负载；证据只增不改。
- 启发式的可采纳性是 A\* 最优承诺的一部分，被写进类型/常量并在不可采纳组合上
  直接返回 `COMPUTATION_FAILED`，而不是静默给出非最优却声称最优的计划。
