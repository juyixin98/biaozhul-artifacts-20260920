# 模块数据与错误契约

本文件约定 `strips_planner` 各模块之间传递的数据结构与错误，禁止跨层
绕过。所有数据类均为 `frozen`；状态转移永远构造新对象。

## 1. 数据契约

### 1.1 语言模型（model.py）

| 类型 | 形状 | 说明 |
|---|---|---|
| `Atom` | `tuple[str, ...]` | `(predicate, arg0, ...)`，全部为基原子或含 `?var` 模板 |
| `State` | `frozenset[Atom]` | 为真原子集合；闭世界，缺席即假 |
| `ActionSchema` | frozen dataclass | 提升算子：`name, parameters, pre_pos, pre_neg, add_effects, del_effects, cost` |
| `GroundAction` | frozen dataclass | 地面实例，另含 `args` 与 `signature`（`name(arg1, arg2)`） |
| `Domain` / `Problem` | frozen dataclass | 校验后的域与问题 |

### 1.2 分层流转

```
dict(JSON)
  -- parser.parse_domain/parse_problem --> Domain / Problem   (只解析形状)
  -- validation.validate               --> None 或抛 ValidationError（收集全部问题）
  -- grounding.ground(domain, problem, limit) --> GroundProblem
                                           （actions: tuple[GroundAction]）
  -- search.search(gp, SearchConfig)   --> SearchResult
  -- executor.execute_plan(gp, plan)   --> ExecutionReport（独立重放）
```

- parser 只负责语法/形状，不做跨字段语义判断；validation 负责静态语义。
- grounding 是唯一把变量替换为对象的地方；它再次强制地面级
  add∩delete=∅，并在实例数超限时抛 `ResourceLimitError`。
- search 不做解析、不做 IO；它只消费 `GroundProblem`，输出
  `SearchResult`，不直接返回 HTTP 概念。
- executor 不调用 search 的任何函数做判定（只共用 `semantics.apply`
  这条转移规则和不可变模型），从而构成对计划的独立验证。

### 1.3 SearchResult 字段契约

| 字段 | 取值/含义 |
|---|---|
| `status` | `solved` / `unsolvable` / `unknown` |
| `plan` | solved 时为 `GroundAction` 元组，其余为空元组 |
| `cost`, `path_length` | solved 时为整数，否则 `None` |
| `optimal_guarantee` | UCS 或 A*+可采纳启发式为 `True`；BFS 仅承诺步数最优；h_add 为 `False` |
| `reason` | 仅 unknown：`node_limit`/`frontier_limit`/`depth_limit`/`time_limit` |
| `expanded/generated/reopened/pruned_dead_end/frontier_peak` | 计数证据 |
| `bounds` | 本次实际生效的四个界限 |
| `trace` | 前若干个被展开状态（默认 ≤500）：seq/depth/g/h/f/action/state_hash/state |
| `final_frontier_sample` | unknown 时前沿前 10 项（g/depth/h/state_hash） |

`unknown` 必须同时满足：未找到解、且未穷尽可达空间。穷尽后无解除外。

### 1.4 执行报告契约（executor.ExecutionReport）

- `valid=True` 当且仅当：每个计划步骤都成功应用且终态满足目标。
- 失败时 `failure = {category, code, step, ...}`，`steps` 保留成功前缀，
  每步含 `state_before/state_after`。
- `code` 取值：`PRECONDITION_FAILED`（state_conflict，附
  `missing_positive`/`present_negative`）、`UNKNOWN_ACTION`、
  `ACTION_ARITY_MISMATCH`（input_error）、`GOAL_NOT_REACHED`
  （state_conflict，附目标缺口）。

### 1.5 状态编码（encoding.py）

- `encode_state`：原子按元组全序排序后用 `" | "` 连接，单目谓词无括号。
- `state_hash`：编码的 SHA-256 前 16 字符，用作 trace/证据稳定标识。
- `decode_state` 是 `encode_state` 的逆，证据库据此精确还原状态。
- 相等判定基于 `frozenset` 本身，去重不依赖字符串，编码仅用于持久化与展示。

## 2. 错误契约

基类 `PlannerError`，四类（errors.py 常量）：

| category | 异常类 | 触发层 | HTTP |
|---|---|---|---|
| `input_error` | `ValidationError` | parser/validation/pipeline options | 422 |
| `state_conflict` | `StateConflictError` | semantics/executor | 409 |
| `resource_exhausted` | `ResourceLimitError` | grounding | 507 |
| `computation_failed` | `ComputationError` | pipeline（计划未通过独立验证等） | 500 |

每个错误带稳定 `code` 字符串与 `details: list[dict]`。HTTP 信封统一为：

```json
{"run_id": "run-...", "error": {"category": "...", "code": "...",
                                "message": "...", "details": []}}
```

搜索界限不是错误（200 + verdict=unknown）。JSON 语法错误使用
`REQUEST_MALFORMED`（422）。未知运行编号使用 `RUN_NOT_FOUND`（404）。

## 3. 证据契约（evidence.py，SQLite）

| 表 | 关键字段 |
|---|---|
| `runs` | run_id(PK)、时间、域/问题名、算法、status、result_status、reason、最优承诺、代价、计数、请求 JSON、计划 JSON、验证 JSON |
| `run_steps` | (run_id, step_index)、action、cost、state_before、state_after（规范编码） |
| `run_trace` | (run_id, seq)、depth/g/h/f、action、state_hash、state_enc |
| `run_errors` | run_id(PK)、category/code/message/details JSON |

- 成功与失败路径都落 `runs`；失败另落 `run_errors`。
- `replay_request(run_id)` 返回原始请求体，`POST /runs/{id}/replay`
  以新 run_id 重跑，两次判定应一致（确定性，无随机搜索）。
- 写入串行化（线程锁 + 单连接），`check_same_thread=False`。

## 4. 流水线不变量（pipeline.py）

1. 搜索给出 solved ⇒ 必须用 executor 独立验证通过，否则降级为
   `computation_failed/INTERNAL_VERIFICATION_FAILED`，绝不返回坏计划。
2. 返回的 `cost` 等于执行器逐步累计代价（二者交叉断言）。
3. 任何 `PlannerError` 都在抛出前写入证据；未预期异常包装为
   `computation_failed`，保留 cause。
