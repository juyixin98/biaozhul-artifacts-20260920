# 实时任务可调度分析服务（Fixed-Priority Response-Time Analysis）

纯后端服务：对**离线固定优先级周期任务集**做可调度性分析。输入每个任务的执行上界
`C`（WCET）、周期 `T`、相对截止期 `D` 和阻塞上界 `B`，用**响应时间迭代（RTA）**
判定每个任务在最坏情况下是否满足截止期，并用一个**独立实现的离散事件调度仿真**作为
参考交叉核对。Python · FastAPI · Pydantic，全程整数精确运算，判定路径不使用浮点。

> 本服务只输出 JSON，不含任何前端页面。

---

## 1. 分析模型与假设（结论的前提，务必先读）

分析结论**有条件地依赖**以下假设；任何违反假设的输入都会被拒绝（HTTP 422）：

1. **单核**：唯一一颗处理器核心。
2. **完全可抢占**：高优先级任务在释放时刻立即抢占低优先级任务。
3. **独立任务**：任务之间无前驱/后继约束，也不分析锁/共享资源协议本身。
   `blocking` 是**调用方提供的保守上界** `B_i`（例如优先级置顶/天花板协议下被
   低优先级任务持有的最长临界区），服务只使用它，**不推导、不证明**该上界是否紧。
4. **周期任务、同步释放**：所有任务在 `t=0` 同时释放（关键时刻 / critical instant），
   后续释放时刻为 `k·T_i`。
5. **固定优先级**：`priority` 为整数，**数值越小优先级越高**。**优先级必须唯一**；
   相等优先级在本模型下被直接拒绝——同优先级需要额外的局内仲裁规则（FIFO/时间片等），
   而本服务使用的响应时间递推在该规则缺失时没有定义，因此不接受平局。
6. **约束截止期**：要求 `D_i ≤ T_i`（`D_i > T_i` 的输入被拒绝）。
7. **离散整数时间**：所有量均为非负整数（时间滴答）。`C_i ≥ 0`、`T_i ≥ 1`、
   `D_i ≥ 0`、`B_i ≥ 0`。

---

## 2. 判定方法：响应时间分析（RTA）

对任务 `i`，记 `hp(i)` 为严格更高优先级任务集合，采用 Joseph & Pandya 递推：

```
R_i^(0)   = C_i + B_i
R_i^(n+1) = C_i + B_i + Σ_{j ∈ hp(i)}  ceil(R_i^n / T_j) · C_j
```

该序列单调不减。终止规则（见 `app/rta.py`）：

| 情况 | 终止条件 | 结论 |
|---|---|---|
| 到达不动点 `R^(n+1) = R^n` 且 `R^n ≤ D_i` | `fixed_point` | **可调度**，`WCRT = R^n` |
| 某个候选 `R^(n+1) > D_i` | `deadline_exceeded` | **不可调度**，记录失败截止期 `failed_deadline = D_i` |
| 候选值超过配置的数值安全上界 | `value_overflow` | **不判成功** |
| 迭代步数上界用尽仍未到不动点 | `iteration_limit` | **不判成功（不收敛）** |

**迭代溢出或不收敛绝不判为可调度。** 截止期一旦被超过就立即确定该任务失败（fail
fast），随后在同一组护栏内继续把迭代跑到不动点，仅作为诊断信息
（`continued_fixed_point`）展示真实最坏响应时间，不改变已经做出的失败结论。

### 为什么低利用率不能作为充分条件

处理器利用率 `U = Σ C_i/T_i` 仅作为**描述性信息**返回，绝不参与判定。固定优先级下
`U ≤ 1`（甚至远低于 1）**既不充分也不必要**地保证可调度性，约束截止期与阻塞都可能使
任务在很低利用率下错过截止期。例：`T1(C=1,T=3)`、`T2(C=2,T=7,D=2)`，
`U = 1/3 + 2/7 ≈ 0.619`，但 `R_0=2`、`R_1 = 2 + ceil(2/3) = 3 > D_2=2`，不可调度。
见 `examples/unschedulable_low_utilization.json`。响应中同时给出 Liu & Layland 的 RM
界 `n(2^(1/n)−1)`，并明确标注它只是**充分非必要**且不参与判定。

---

## 3. 离散事件调度参考（交叉核对）

`app/simulation.py` 是对同一调度模型的**独立实现**：整数滴答的事件驱动、固定优先级、
可抢占调度，按 `k·T_i` 释放作业，在释放点立即抢占，绝对截止期为 `release + D`。

- 默认仿真窗口为一个**超周期**（各周期的最小公倍数 LCM），并排空窗口内释放的作业。
- 基线运行把所有 `B_i` 当作 0，得到纯固定优先级抢占调度；对每个提供了 `blocking` 的
  任务，再单独运行一次「关键时刻非抢占阻塞块」仿真（`[0,B)` 内核不可用），以复现 RTA
  的 `B_i` 项。
- 比较**关键时刻（t=0）首个作业的仿真响应时间**与 RTA 不动点：
  `agree` / `disagree` / `inconclusive`。
- 仿真有显式的释放数量与滴答数护栏；命中护栏则标记 `truncated`，交叉核对如实报
  `inconclusive`，绝不猜测。

---

## 4. 目录结构

```
.
├── app/
│   ├── main.py          # FastAPI 入口、端点、模型违反时的统一拒绝
│   ├── models.py        # 请求/响应模型与输入校验（模型假设）
│   ├── rta.py           # 响应时间迭代（判定核心，整数运算）
│   ├── simulation.py    # 离散事件固定优先级调度参考
│   └── analyzer.py      # 编排：RTA + 仿真交叉核对 + 利用率 + SHA-256
├── examples/            # 已知可调度 / 不可调度 / 非法输入样例
├── tests/               # pytest 自动化测试（51 个）
├── requirements.txt     # 直接依赖（建议安装）
├── requirements.lock    # pip freeze 全量锁定依赖（可复现）
├── pytest.ini
└── README.md
```

---

## 5. 本地启动

需要 Python 3.10+（开发于 3.12）。

```bash
# 1) 创建并激活虚拟环境
python3 -m venv .venv
source .venv/bin/activate

# 2) 安装依赖（可复现用锁文件）
pip install -r requirements.lock
#    或仅安装直接依赖： pip install -r requirements.txt

# 3) 启动服务
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

启动后：

- 交互文档（Swagger UI）：http://127.0.0.1:8000/docs
- 健康检查：`GET /health`

---

## 6. HTTP 接口

### `POST /api/v1/analyze`

请求体：

```json
{
  "tasks": [
    {"id": "T1", "wcet": 1, "period": 4,  "deadline": 3,  "blocking": 0, "priority": 1},
    {"id": "T2", "wcet": 2, "period": 8,  "deadline": 8,  "blocking": 1, "priority": 2},
    {"id": "T3", "wcet": 3, "period": 12, "deadline": 12, "blocking": 0, "priority": 3}
  ],
  "options": {"run_simulation": true, "max_iterations": 10000, "max_response_ticks": 1000000000000}
}
```

字段：`id`（唯一非空，字母数字 `._-`）、`wcet`、`period`、`deadline`、`blocking`
（默认 0）、`priority`（唯一，越小越高）。`options` 可省略。

响应要点：

- 顶层 `schedulable`：所有任务都满足截止期才为 `true`。
- `tasks[]`：每个任务的
  - `interference_sources`：每个高优先级干扰源及其 `ceil(R/T_j)·C_j` 形式；
  - `iterations[]`：完整迭代过程 `n, r, interference{task: ticks}, total_interference, next_r`；
  - `response_time`（不动点 WCRT）、`slack = D − WCRT`；
  - `terminal_condition`、`failed_deadline`、`failure_detail`、`continued_fixed_point`；
  - `rta_crosscheck`：RTA 与仿真对该任务关键时刻的一致性。
- `simulation`：超周期、释放/完成作业数、截止期错过数、`first_miss`、各任务 t=0 作业
  响应时间、零阻塞基线，以及总体 `crosscheck`。
- `utilization`：仅描述用的利用率及「**不是充分条件**」的明确说明。
- `integrity`：对规范化请求体真实计算的 **SHA-256**（见第 8 节）。

模型违反（`D>T`、优先级重复、id 重复、字段缺失/越界/非整数等）返回 **HTTP 422**，
体形如：

```json
{"error": "model_violation", "code": "invalid_input", "detail": "…原因…"}
```

请求体不是合法 JSON 返回 400（`code: invalid_json`）。

### 其它端点

- `GET /`：服务与端点清单。
- `GET /health`：存活检查。
- `POST /api/v1/digest`：对任意 JSON 载荷做规范化 SHA-256（工具端点）。

---

## 7. 验收命令

```bash
source .venv/bin/activate

# (a) 自动化测试：51 passed
python -m pytest

# (b) 已知可调度集合 -> schedulable=true，交叉核对 agree
curl -s -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/schedulable.json

# (c) 低利用率却不可调度 -> schedulable=false，T2 failed_deadline=2
curl -s -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/unschedulable_low_utilization.json

# (d) 阻塞导致错过截止期 -> schedulable=false，T2 R_1=7>D=5
curl -s -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/unschedulable_blocking.json

# (e) 违反模型（D>T）-> HTTP 422 model_violation
curl -i -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H 'Content-Type: application/json' \
  --data @examples/invalid_deadline_gt_period.json
```

### 内置参考集合（与离散仿真逐一对照）

| 样例 | 任务 (C,T,D,B) | U | RTA 结论 |
|---|---|---|---|
| `schedulable.json` | T1(1,4,3,0), T2(2,8,8,1), T3(3,12,12,0) | 0.75 | 全部可调度，WCRT = 1/4/7，仿真 agree |
| `unschedulable_low_utilization.json` | T1(1,3,3,0), T2(2,7,2,0) | 0.619 | T2 失败：R1=3>D=2，仿真有错过 |
| `unschedulable_blocking.json` | T1(2,10,10,0), T2(2,5,5,3) | 0.60 | T2 失败：R0=5→R1=7>D=5，加阻塞仿真 agree |
| `invalid_deadline_gt_period.json` | T1(1,4,5,0) | — | 拒绝：D>T（HTTP 422） |

---

## 8. 完整性校验（真实密码学运算）

每次 `/api/v1/analyze` 都对**接收到的请求**做真实 SHA-256：Pydantic 校验并规范化
（补全 `options` 等默认字段）后，用 `sort_keys=True`、`separators=(',',':')` 编码为
UTF-8 JSON，再 `hashlib.sha256(...).hexdigest()`。

响应 `integrity` 中包含：

- `sha256`：规范化载荷的真实摘要；
- `canonical_payload`：被哈希的那份**规范化 JSON 对象**（默认值已展开）——任何人无需
  知道默认值，对它重新规范化并哈希即可复算；
- `canonical_encoding`、`canonical_payload_bytes`。

独立复算（对服务回传的 `canonical_payload`）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/analyze \
  -H 'Content-Type: application/json' --data @examples/schedulable.json \
| python - <<'PY'
import sys, json, hashlib
d = json.load(sys.stdin)
integ = d["integrity"]
recomputed = hashlib.sha256(
    json.dumps(integ["canonical_payload"], sort_keys=True, separators=(",", ":")).encode()
).hexdigest()
assert recomputed == integ["sha256"], "digest mismatch"
print("verified", integ["sha256"])
PY
```

规范化只对对象键排序、去除空白；**数组顺序是语义的一部分**，不做重排。因此修改任意一个
数值都会改变摘要，而仅改变 JSON 中对象键的书写顺序不会。`/api/v1/digest` 对任意原始
JSON 提供同样能力（该端点直接哈希传入对象，不做默认值展开）。哈希为实际计算（非占位）。

---

## 9. 设计说明与边界

- 判定全部使用 Python 整数，无浮点参与；`utilization` 中的浮点仅用于展示，不影响结论。
- 迭代护栏 `max_iterations`（默认 10000）与 `max_response_ticks`（默认 10¹²）是防御性
  上限，命中即 `iteration_limit` / `value_overflow`，按失败处理。
- 在约束截止期 `D≤T` 下，过载（U>1）的任务集会在首个作业上体现为 `deadline_exceeded`；
  护栏保证即便构造极端输入也不会无限循环或给出虚假成功。
- 仿真是**参考**而非证明：超大周期会使超周期巨大，此时受 `tick_cap` /
  `released_jobs_cap` 限制会标 `truncated` 并报 `inconclusive`，属于如实报告。
- 本服务不做多核、亲和性、抖动（jitter）、释放漂移、非周期/偶发任务或资源锁协议的
  完整分析；`B_i` 的正确性与紧致性由调用方负责。
