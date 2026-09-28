# STRIPS 离线规划服务

一个基于 **Python + FastAPI + SQLite** 的地面（grounded）STRIPS 规划后端：
支持正/负前置条件、增/删效果，提供代价最优与非最优两种搜索承诺，
所有返回的动作序列都由**独立执行器**逐步重放验证，无法在界限内判定时
明确返回 `unknown` 与触发的界限，而不是武断回答无解。

所有数据均为本地合成夹具（车间资源操作域、路由图域），无外部账号依赖。

---

## 1. 快速开始

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt          # 或 pip install -r requirements-lock.txt

# 运行测试（108 个用例，覆盖率 ≥ 80% 门禁）
python -m pytest tests/ --cov=strips_planner

# 启动服务
python -m strips_planner.service             # 默认 127.0.0.1:8000

# 另一个终端：调用示例
bash examples/curl_examples.sh
python examples/python_client.py
```

环境变量：`PLANNER_HOST` / `PLANNER_PORT` / `PLANNER_DB`（默认
`data/evidence.db`）/ `PLANNER_LOG`（默认 `logs/planner.log`）。

## 2. 规则语言（JSON）

```json
{
  "domain": {
    "name": "resource-ops",
    "actions": [{
      "name": "move",
      "parameters": ["?w", "?from", "?to"],
      "preconditions": {"pos": ["at(?w,?from)", "clear(?to)"],
                        "neg": ["sealed(?to)", "at(?w,?to)"]},
      "add":    ["at(?w,?to)", "clear(?from)"],
      "delete": ["at(?w,?from)", "clear(?to)"],
      "cost": 1
    }]
  },
  "problem": {
    "name": "demo",
    "objects": ["w1", "bench", "dock"],
    "init":  ["at(w1,bench)", "clear(dock)"],
    "goal":  {"pos": ["at(w1,dock)"], "neg": []}
  },
  "options": {"algorithm": "astar", "heuristic": "h_max"}
}
```

- 变量以 `?` 前缀标识；字面量写作 `pred(a,b)`，否定写作 `~pred(a,b)`；
  `preconditions`/`goal` 既接受 `{"pos":[...],"neg":[...]}` 也接受带符号的扁平列表。
- 闭世界假设：状态是“为真原子”的集合，负条件按“原子缺席”判定。
- 动作为**正整数代价**；零代价不允许（避免零代价环）。

## 3. 固定的语义规则（不可悄悄改变）

1. **同一前态判定**：可应用性只针对一个前驱状态；正负条件一次性检查。
2. **增删冲突规则固定**：后继状态一次性由
   `s' = (s − delete) ∪ add` 计算，绝不把增/删当作对“正在演化的状态”
   的顺序修改。模板级与实例化级都禁止同一原子既增又删
   （`EFFECT_ADD_DELETE_CONFLICT`），因此 add-wins/delete-wins 不会产生分歧。
3. **状态编码去重**：状态是原子的 `frozenset`，规范编码排序后拼接，
   SHA-256 截断作为证据外键；同一状态只保留一个最优 `g`，循环动作
   （如 `toggle_power`/`cut_power`）不会产生重复展开。
4. **最优性承诺显式化**：

   | 算法/启发式 | 承诺 |
   |---|---|
   | BFS | **步数最少**（不承诺代价最优；仍如实累计代价） |
   | UCS（h=0） | **代价最优** |
   | A* + `h_max` | **代价最优**（h_max 可采纳、一致） |
   | A* + `h_add` | 计划有效但**不承诺最优**（`optimal_guarantee=false`） |

5. **判不动就说不知道**：节点数 / 前沿 / 深度 / 时间任一界限触顶，
   返回 HTTP 200 + `verdict="unknown"` 与 `reason`，并附前沿样本；
   只有完整穷尽可达空间才返回 `unsolvable`。深度限制截断了空间时同样
   返回 `unknown/depth_limit`。
6. **独立执行器验证**：搜索返回的计划由 `executor` 从初始状态逐步重放，
   逐步记录前态/后态；任一步失败或终态未达目标，计划判无效。若搜索
   产出的计划连独立执行器都无法通过，服务报 `computation_failed`（这是
   服务缺陷，而非调用方问题）。

## 4. 错误分类（可区分的四类）

| category | HTTP | 含义 | 示例 code |
|---|---|---|---|
| `input_error` | 422 | 规则/请求静态不合法 | `PARAM_MISSING`、`UNBOUND_VARIABLE`、`EFFECT_ADD_DELETE_CONFLICT` |
| `state_conflict` | 409 | 对具体状态违反动态规则 | `PRECONDITION_FAILED`（含缺失正条件/存在负条件明细） |
| `resource_exhausted` | 507 | 服务端准备阶段触顶（grounding 爆炸） | `GROUNDING_LIMIT` |
| `computation_failed` | 500 | 服务自身缺陷 | `INTERNAL_VERIFICATION_FAILED` |

注意：搜索界限触顶**不是错误**，是 `unknown` 正常结果（200）。

## 5. HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `POST /plan` | 提交规划请求，返回判定、计划、独立验证结果、搜索计数与轨迹 |
| `GET /runs?limit=` | 运行编号列表 |
| `GET /runs/{id}` | 单次运行（请求体、判定、计划、错误信封） |
| `GET /runs/{id}/steps` | 逐步执行证据（每步前态/后态） |
| `GET /runs/{id}/trace` | 关键中间搜索状态（g/h/f、深度、生成动作） |
| `POST /runs/{id}/replay` | 用存证的原始请求以**新运行编号**重放 |

运行编号形如 `run-20260928T004230-a1c2b3d4`（时间戳 + 随机后缀），
足以在日志/SQLite 中重放问题。

## 6. 工程结构（模块边界）

```
strips_planner/
  model.py        数据模型（frozen dataclass，Atom/State/GroundAction）
  parser.py       JSON 规则语言解析
  validation.py   静态语义校验（一次性收集所有问题）
  grounding.py    提升动作 → 地面实例（含 grounding 资源界限）
  semantics.py    转移语义（同一前态 + 固定增删规则）
  heuristics.py   zero / h_max（可采纳）/ h_add（不可采纳，明确标注）
  search.py       BFS / UCS / A*，去重、界限、unknown 语义、轨迹
  executor.py     独立计划执行器（逐步重放，不与搜索共享判定代码）
  evidence.py     SQLite 证据库（runs/steps/trace/errors，可重放）
  pipeline.py     编排：解析→校验→grounding→搜索→独立验证→落库
  api.py          HTTP 路由与错误信封（直接消费 JSON，保留全部稳定错误码）
  service.py      应用工厂/入口
  encoding.py     规范状态编码/解码
  errors.py       四类错误分类法与稳定 code
fixtures/         合成域与问题（可解/无解/循环/多代价路径）
tests/            108 个测试，含独立参考实现 oracle 与 40 组随机器
examples/         curl / Python 调用示例与请求体
docs/             契约与复现文档
```

## 7. 验收测试要点（不只是“接口能调”）

- 合成**资源操作域**：可解问题断言确切代价 8、6 步及动作序列。
- **无解目标**两种：没有任何动作能产生目标谓词；负条件封锁泊位。
  断言 `unsolvable` 且穷尽展开计数足够大。
- **循环动作**：电源开合循环被去重折叠，3 次展开内得解。
- **多个代价路径**：路由图域，廉价 3 跳（代价 3）vs 昂贵直达（代价 5）。
  BFS 断言步数 1/代价 5，UCS 与 A*(h_max) 断言代价 3/3 步，
  A*(h_add) 断言 `optimal_guarantee=false`。
- **独立参考实现**：`tests/test_reference_oracle.py` 自带一个与被测内核
  零代码共享的 oracle（自己解析 JSON、itertools grounding、整数位掩码、
  穷举 BFS/Dijkstra）。对全部夹具及 **40 组随机生成实例**做差分比对：
  判定、最优代价、计划可执行性、h_max 可采纳性逐项断言。参考答案不是
  由被测核心生成的。
- **失败类别**：输入错误 422、grounding 资源耗尽 507、执行器前置条件
  冲突定位到具体步骤、搜索界限返回 unknown。

## 8. 文档索引

- [docs/contracts.md](docs/contracts.md) — 模块间数据与错误契约
- [docs/reproduction.md](docs/reproduction.md) — 复现步骤与已留存结果
