# 有限动作集合的时间规划服务（Temporal Planner）

在整数时间网格上，对**有限已知动作集合**进行时间规划，每个动作具有：

- **开始条件**（precondition，开始时点检查）；
- **持续不变量**（invariant，执行区间内**每个内部网格点**检查，而非仅两端）；
- **结束效果**（effects，结束时点应用；支持零时长动作）。

技术栈：**Python 3.12 + FastAPI + SQLite**。所有输入均为本地合成夹具，无外部业务依赖。

---

## 1. 工程结构（按四层组织）

```
app/
  rules/                 # 规则语言层
    time.py              #   整数网格 + 统一半开区间 [start,end) 算术
    conditions.py        #   条件/效果小型语言（fact/all/any/not；=、+=、-=）
    models.py            #   Action / Problem / Plan / ReplayResult / SearchResult
    errors.py            #   稳定的失败类别码（FailureCategory）
    loader.py            #   YAML/JSON 边界校验加载
  planner/               # 推理 / 规划内核
    replay.py            #   独立时间轴回放引擎（唯一语义裁决者，产出完整证据时间轴）
    enumerate.py         #   小时间网格穷举参考（扁平多重集枚举，仅做保完备结构剪枝）
    solver.py            #   带节点预算的时序 DFS（先求可行，再逐界证明最优）
  storage/               # 证据存储层（SQLite：运行、时间轴事件、违例、交叉校验）
  api/                   # 查询接口层（FastAPI：/api/solve、/api/replay、/api/reference、runs）
  config.py              # 独立配置层（环境变量 + 显式覆盖 + 版本信息）

tests/
  oracle.py              # ★ 测试侧独立预言机：不 import 任何 app 代码，从零重放/搜索
  fixtures/*.yaml        # 本地合成问题夹具（drone / workshop / reactor）
  test_*.py              # 97 个具体断言测试
examples/                # JSON 请求样例 + 端到端 Python 脚本
requirements.txt         # 锁定的关键依赖版本
```

**为什么不是单文件/调用壳/固定返回值**：语义裁决（replay）、两套结构不同的
搜索（穷举参考、预算 DFS）、第三套测试侧预言机彼此独立；任何返回的计划都
必须再次通过 `replay` 独立验证，参考答案也不是由被测核心自身生成的。

---

## 2. 时间语义契约（关键设计）

时间是非负整数网格，视界为 `H`，所有动作必须在 `H` 前结束。

### 2.1 资源占用统一为半开区间 `[start, end)`

- 在网格点 `start … end-1` 占用资源，**在 `end` 时点释放**；
- `[0,2)` 与 `[2,4)` **不冲突**（边界释放，boundary release）；
- 零时长区间 `[t,t)` 为空，不占用任何资源，也不与任何区间冲突。

### 2.2 同一时点的固定相位顺序

同时刻事件不是任意排序，而是按文档化的固定相位执行（策略名
`ENDS_FIRST_REJECT_WRITE_WRITES`）：

1. **END**：在 `t` 结束的正时长动作，按声明顺序施加结束效果；
2. **PRE**：在 `t` 开始的所有动作，针对**同一**状态检查开始条件
   （因此 `t` 开始者能看到 `t` 释放的结束效果，但看不到同时刻零时长动作的效果）；
3. **ZERO**：在 `t` 开始的零时长动作施加效果；
4. **INV**：对在区段 `[t,t+1)` 上活动（`start ≤ t < end`）的所有动作，
   针对最终区段状态检查持续不变量。

**写/写冲突拒绝**：两个不同动作实例在同一时点写同一事实时，直接判定为
`SIMULTANEOUS_CONFLICT`，而不是偷偷规定先后。

### 2.3 不变量在每个内部点检查

不变量在 `start … end-1` 的**每一个**网格点检查。例如动作 A 执行中，
动作 B 在中途结束并把 A 依赖的事实清零，失效会发生在内部区段 `[t,t+1)`，
仅检查开始/结束两端会漏判——本服务会在该内部点报 `INVARIANT_VIOLATION`。
零时长动作没有内部点，其不变量为空真。

### 2.4 失败类别（稳定字符串契约，测试据此断言）

| 类别 | 含义 |
|---|---|
| `PRECONDITION_VIOLATION` | 开始条件在开始时点不成立 |
| `INVARIANT_VIOLATION` | 持续不变量在某内部区段失效 |
| `GOAL_NOT_REACHED` | 视界时刻目标不成立（独立成类） |
| `RESOURCE_CONFLICT` | 半开区间资源重叠 |
| `SIMULTANEOUS_CONFLICT` | 同时刻对同一事实的歧义写 |
| `SCHEDULE_INVALID` | 未知动作 / 时长越界 / 超出视界 |
| `INVALID_INPUT` | 请求或问题定义不合法（HTTP 400） |
| `INFEASIBLE` / `BUDGET_EXHAUSTED` 等 | 搜索层状态（见下） |

---

## 3. 求解与预算语义

`POST /api/solve` 先找**任意可行计划**，再按 makespan 上界
`0,1,…,best-1` 升序穷举证明最优。所有搜索共享**节点展开预算**：

| `status` | 含义 | `optimal` | 是否返回计划 |
|---|---|---|---|
| `OPTIMAL` | 找到可行计划且所有更小 makespan 界已穷尽证伪 | `true` | 是 |
| `FEASIBLE_UNPROVEN` | 已有可行计划，但预算在证明某界时到期，**最优未证明** | `false` | 是（仍经 replay 验证有效） |
| `INFEASIBLE` | 在显式步数/出现次数上限内穷尽，证明无解 | `false` | 否 |
| `NO_PLAN_WITHIN_BUDGET` | 预算在找到任何可行计划前到期；**可行性未知**，不冒充失败也不冒充成功 | `false` | 否 |

`reason` 字段会明确指出未完成的是哪个界、哪些界已证明不可行、已展开节点数。

---

## 4. 安装与运行

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt

# 启动 HTTP 服务（默认 data/planner.db，可用环境变量覆盖）
PLANNER_DB_PATH=data/planner.db \
python3 -m uvicorn app.api.app:create_app --factory --host 127.0.0.1 --port 8000
```

配置（环境变量）：`PLANNER_DB_PATH`、`PLANNER_LOG_LEVEL`、
`PLANNER_DEFAULT_BUDGET`、`PLANNER_DEFAULT_MAX_STEPS`。

### 示例调用

```bash
# 最优求解 + 小网格穷举交叉校验
curl -s -X POST http://127.0.0.1:8000/api/solve \
  -H 'content-type: application/json' \
  --data @examples/drone_request.json

# 独立回放一个“中途不变量失效”的计划（返回 INVALID 与具体违例时点/类别）
curl -s -X POST http://127.0.0.1:8000/api/replay \
  -H 'content-type: application/json' \
  --data @examples/reactor_invalid_plan.json

# 按运行身份取回完整证据时间轴 / 违例
curl -s http://127.0.0.1:8000/api/runs/<run_id>/events
curl -s http://127.0.0.1:8000/api/runs/<run_id>/violations
```

端到端脚本（求解、回放无效计划、预算到期、从 SQLite 取证）：

```bash
python examples/run_example.py
```

接口一览：`GET /health`、`GET /version`、`POST /api/solve`、
`POST /api/replay`、`POST /api/reference`、`GET /api/runs`、
`GET /api/runs/{id}`、`.../events`、`.../violations`。
交互式文档见 `http://127.0.0.1:8000/docs`。

---

## 5. 问题定义格式（YAML/JSON）

```yaml
name: drone-delivery
horizon: 5
initial: {battery: 3, delivered: 0}
goal: {fact: {fact: delivered, op: "==", value: 2}}
resources: [sky]
actions:
  - name: fly
    duration_min: 2            # 可用 duration_max 给定时长区间
    resources: [sky]
    precondition: {fact: {fact: battery, op: ">=", value: 2}}
    invariant:    {fact: {fact: battery, op: ">=", value: 1}}
    effects:
      - {fact: battery, op: "-=", value: 2}
      - {fact: delivered, op: "+=", value: 1}
  - name: recharge
    duration_min: 0            # 零时长动作
    effects: [{fact: battery, op: "+=", value: 2}]
```

条件支持 `fact`（运算符 `== != >= <= > <`）、`all`、`any`、`not`、
`{always: true}`；未设置的事实按闭世界读作 `0`；事实值为整数或布尔，
布尔与整数间的序比较/算术会在边界被拒绝。

---

## 6. 测试与复现

```bash
# 全量（97 个测试），约 1 分钟
python3 -m pytest -q

# 带覆盖率（当前 app 包行覆盖率约 94%）
python3 -m pytest -q --cov=app --cov-report=term-missing

# 按标记选择：semantics / reference / solver / budget / differential / storage / api
python3 -m pytest -m budget -q
```

测试如何回答题面中的具体问题：

- **小时间网格穷举参考**：`test_reference_enumerator.py` 断言最优 makespan
  的具体数值（drone=4、workshop=4），并校验返回计划确实 replay 有效；
- **中途条件失效 / 零时长 / 边界释放**：`test_replay_semantics.py` 断言
  具体违例类别与时点（如内部点 t=1/t=2 的 `INVARIANT_VIOLATION`、
  半开区间 `[0,2)+[2,4)` 无 `RESOURCE_CONFLICT`、零时长效果对同时刻开始者
  不可见）；
- **独立回放验证完整时间轴**：`tests/oracle.py` 完全不 import `app`，
  `test_differential.py` 用它对 300 个随机计划逐条比对
  有效性/类别化违例/最终状态/每个改状态相位，并让三套搜索引擎
  （扁平穷举、预算 DFS、oracle 的按时间分桶搜索）在夹具与随机问题上一致；
- **断言具体结果与失败类别**：没有“接口能调通”式断言；
- **预算到期**：`test_budget_semantics.py` 用固定预算（800 节点）稳定复现
  `FEASIBLE_UNPROVEN`，断言可行计划仍返回、`optimal=false`、`reason` 点名
  未完成界；3 节点复现 `NO_PLAN_WITHIN_BUDGET`。

**失败日志如何关联输入/运行身份**：每个测试有唯一 `run_id`（写入日志，
如 `run=test-30fa1d9ed42c`）；失败报告附 “reproduction context” 段，含
测试节点 ID、run_id、引擎与 python/fastapi/pydantic 版本。HTTP 与 SQLite
证据中的每次运行有 `run_id` 与对输入规范化 JSON 的 SHA-256
`input_fingerprint`，事件的 `detail` 记录判定依据（相位、相关事实快照）。
未知/异常状态绝不统一返回成功：400 携带 `INVALID_INPUT`、404 携带
`NOT_FOUND`、500 携带 `INTERNAL_ERROR` 与 incident id。

---

## 7. 已验证内容与剩余限制

已真实执行验证：97 个 pytest 全绿；`examples/run_example.py` 端到端跑通；
真实 `uvicorn` 服务下 curl 验证了 solve/replay/events/404/400 路径。

剩余限制（如实说明）：

1. **有限视界、有限动作集合、整数网格**：不支持连续时间、动态新增动作类型。
2. **搜索有显式上限**：`max_steps`（默认 32）与每动作出现次数
   （默认 `horizon+2`）界定搜索空间；超出上限的解不在完备性范围内，
   预算/上限到期时状态会明确标注为“未证明”，不会谎称 `INFEASIBLE`。
3. **优化目标固定为最小 makespan**；不支持任意代价函数或多目标。
4. **同步 SQLite**：单机 WAL，写操作加进程内锁；适合本地/单机证据规模，
   非高并发生产部署。
5. 参考穷举与差分搜索面向**小网格**（夹具中 horizon ≤ 6）；大问题以预算
   DFS 为主，穷举仅用于小网格交叉校验。
6. 条件语言不含量词/外部函数求值；效果为确定性整数/布尔赋值与加减。
