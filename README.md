# 实时任务可调度分析服务（Fixed-Priority RTA Service）

纯后端的**离线固定优先级周期任务可调度性分析**服务：输入任务的执行上界
`C`、周期 `T`、相对截止期 `D` 与阻塞上界 `B`，使用**响应时间分析
（Response-Time Analysis, RTA）迭代**判断每个任务在最坏情况下能否在截止期
前完成，并用一套**独立实现的离散事件调度器**在临界瞬时相位下做交叉对照。

Python 3.12 + FastAPI，无前端页面，无数据库。

---

## 1. 调度模型与假设（服务硬约束）

1. **单处理器（单核）、固定优先级、完全可抢占**调度；调度与上下文切换开销为 0。
2. **任务相互独立**：无释放依赖、无执行顺序依赖；不存在任务间抖动与释放偏移。
   唯一允许的共享资源效应是 `B_i`：在优先级继承/优先级天花板协议下，任务
   `τ_i` 被低优先级任务的非抢占临界区阻塞**至多一次**的时间上界。
3. 每个任务周期性释放作业，相邻释放间隔恰为 `T_i`；**受限截止期**
   `D_i <= T_i`（输入违反即拒绝，见 §6）。
4. `C_i` 为最坏情况执行时间上界；作业在完成前的处理器需求至多 `C_i`。
5. **临界瞬时（critical instant）**：所有高优先级任务与目标任务同时释放，
   且目标任务恰好遭遇一次最大阻塞。经典 RTA 结论：该相位下的响应时间即
   最坏响应时间。
6. 所有时间参数为**正整数 tick**。优先级规则**固定为速率单调 RM**
   （周期越短优先级越高）；同周期任务按**输入顺序**仲裁（先出现者优先级
   更高）。服务不接受调用方传入自定义优先级或其他策略（如 EDF）。

## 2. 计算方法

标准 RTA 不动点迭代（`hp(i)` 为严格高优先级任务集合）：

```
R_i^(0) = C_i + B_i
R_i^(n+1) = C_i + B_i + Σ_{j ∈ hp(i)} ceil(R_i^(n) / T_j) · C_j
```

判定规则（右侧函数关于 x 单调不减，故序列单调不减）：

| 情况 | 判定 | 输出 |
|---|---|---|
| 不动点 `R^(n+1)=R^(n)` 且 `R <= D_i` | **可调度** `converged` | 最坏响应 `R` |
| 首次出现 `R^(n) > D_i` | **截止期错失** `deadline_miss` | 首个超限迭代值与**失败幅度 `R-D`** |
| 达到保护性步数上限仍非不动点 | **不收敛** `non_converged` | **绝不判成功** |

- **低平均利用率不是充分条件**：服务只以 RTA 不动点判定，利用率
  `U=ΣC/T` 仅作信息展示（精确分数 + 浮点）。示例
  `unschedulable_util_under_one.json` 中 `U=23/24≈0.958<1`，最低优先级
  任务仍错失截止期。
- 每一步的 `r_prev`、总干扰、`r_next`、以及**每个高优先级任务的干扰
  明细**（`ceil(R/T_j)` 释放次数与干扰量）都在响应中完整给出。

### 离散调度交叉对照

服务内另有一个与 RTA **相互独立**的事件驱动抢占调度器（
`app/simulation.py`）：

- **全局调度**：临界瞬时相位下所有任务的独立作业在一个超周期
  （`lcm(T_i)`，封顶 200000 tick）内的 release/start/finish/response、
  截止期错失数与超周期末积压；
- **逐任务阻塞仿真**：对每个 `τ_i` 注入一个在 t=0 持有锁的“低优先级
  任务”，它继承 `τ_i` 的优先级（高优先级任务可抢占它，`τ_i` 必须等其
  跑完 `B_i`）——这正是阻塞项进入 RTA 递推的语义。该作业的离散响应
  时间与 RTA 不动点**精确相等**（可调度任务）；错失任务则一致地表现为
  到点未完成或在 `D` 后完成。任何不一致都会在 `simulation.mismatch` 中
  如实暴露，且整体 `matches_rta=false`。

另含 300 组随机任务集对照测试（固定随机种子，见
`tests/test_simulation.py`）。

## 3. 目录结构

```
app/
  main.py         FastAPI 路由：GET /、GET /api/health、POST /api/analyze
  model.py        请求/响应 Pydantic 模型与模型约束校验
  rta.py          RTA 迭代核心（RM 赋优先级、不动点、判定）
  simulation.py   独立离散事件调度器与交叉对照
  integrity.py    真实 SHA-256 与 HMAC-SHA256（hmac/hashlib，无占位）
examples/         手工核算的示例输入（可调度/不可调度/非法）
tests/            pytest 自动化测试（35 个用例）
requirements.txt  直接依赖（顶层约束）
requirements.lock 完整锁定版本（pip freeze，验收使用）
```

## 4. 本地启动

```bash
cd b
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock   # 按锁定版本安装
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18077
```

启动后：

- `GET  http://127.0.0.1:18077/`          模型、假设、公式与服务边界
- `GET  http://127.0.0.1:18077/api/health` 健康检查与密钥来源
- `POST http://127.0.0.1:18077/api/analyze` 提交任务集进行分析

> 如端口被占用，自行更换 `--port`。如本机有 HTTP 代理，curl 请加
> `--noproxy '*'`。

## 5. 请求与响应

请求体（字段同时接受 `C/T/D/B` 别名与 `wcet/period/deadline/blocking`
全名；`B` 缺省为 0，`priority_policy` 缺省且仅允许 `"RM"`）：

```json
{
  "priority_policy": "RM",
  "tasks": [
    {"id": "tau1", "C": 1, "T": 4, "D": 4, "B": 0},
    {"id": "tau2", "C": 2, "T": 6, "D": 6, "B": 1},
    {"id": "tau3", "C": 1, "T": 8, "D": 8, "B": 1}
  ]
}
```

分析示例：

```bash
curl -s --noproxy '*' http://127.0.0.1:18077/ \
  | .venv/bin/python -m json.tool

curl -s --noproxy '*' -X POST http://127.0.0.1:18077/api/analyze \
  -H 'Content-Type: application/json' \
  -d @examples/schedulable.json | .venv/bin/python -m json.tool
```

响应关键字段：

- `schedulable`：全部任务均收敛到 `R<=D` 才为 `true`；
- `model / assumptions / priority_policy / tie_break_rule`：模型声明；
- `tasks[].priority_rank`（1=最高）、`higher_priority_tasks`（干扰来源）、
  `iterations[]`（每步干扰明细与迭代过程）、`outcome`、`final_rt`、
  `deadline_miss`（失败幅度）；
- `utilization`：精确分数利用率与 RM 充分界（仅信息性）；
- `simulation`：离散调度作业表、每任务最大观测响应、错失数、积压、
  `matches_rta` 与可能的 `mismatch`；
- `sha256` / `hmac_sha256`：对**去掉这两个字段后的规范 JSON**
  （`sort_keys`、紧凑分隔、UTF-8）真实计算的摘要与 HMAC，供验签/审计。

### 完整性签名（真实密码学操作）

- 未设置环境变量时，进程启动用 `secrets.token_bytes(32)` 生成**一次性
  临时密钥**（重启即失效，`/api/health` 中显示 `ephemeral:...`）；
- 设置 `RTA_HMAC_KEY`（≥16 字节）则使用该密钥，便于多副本或客户端验签：

```bash
RTA_HMAC_KEY=your-shared-secret-min-16-bytes \
  .venv/bin/uvicorn app.main:app --port 18077
```

测试 `tests/test_api.py::test_response_carries_real_crypto_and_verifies`
会独立重算 HMAC 验证通过，并断言篡改响应体后验签失败。

## 6. 输入拒绝策略（违反模型即 422，不进入计算）

- `D_i > T_i`（违反受限截止期）；`C/T/D <= 0`、`B < 0` 或超过安全上界；
- 任务列表为空、超过 64 个任务、任务 id 为空或重复；
- `priority_policy` 不是 `"RM"`（自定义/其他策略一律拒绝）；
- 请求中出现模型外多余字段（`extra=forbid`）。

安全边界：任务数 ≤ 64，单个时间参数 ≤ 1 000 000 tick。

## 7. 验收命令

```bash
# 1) 按锁定依赖建环境
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock

# 2) 全量自动化测试（RTA 单测 + 校验 + 离散调度对照 + API + HMAC）
.venv/bin/pytest

# 3) 启动服务
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 18077 &

# 4) 可调度集合 → schedulable=true, simulation.matches_rta=true
curl -s --noproxy '*' -X POST http://127.0.0.1:18077/api/analyze \
  -H 'Content-Type: application/json' -d @examples/schedulable.json \
  | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); \
assert d['schedulable'] and d['simulation']['matches_rta']; print('PASS schedulable')"

# 5) U<1 但不可调度 → schedulable=false 且利用率 < 1
curl -s --noproxy '*' -X POST http://127.0.0.1:18077/api/analyze \
  -H 'Content-Type: application/json' -d @examples/unschedulable_util_under_one.json \
  | .venv/bin/python -c "import json,sys; d=json.load(sys.stdin); \
assert (not d['schedulable']) and d['utilization']['total_utilization_float'] < 1 \
and d['simulation']['matches_rta']; print('PASS unschedulable despite U<1')"

# 6) 非法输入 D>T → HTTP 422
curl -s --noproxy '*' -o /dev/null -w '%{http_code}\n' -X POST \
  http://127.0.0.1:18077/api/analyze -H 'Content-Type: application/json' \
  -d @examples/invalid_deadline_gt_period.json   # 期望 422
```

## 8. 示例集合（均手工核算）

| 文件 | 任务集 | 结果 |
|---|---|---|
| `examples/schedulable.json` | (C,T,D,B)：(1,4,4,0)、(2,6,6,1)、(1,8,8,1) | 全部可调度，R=1/4/6，U=17/24 |
| `examples/unschedulable_util_under_one.json` | (1,4)、(2,6)、(3,8) | τ3：R 首次越界 9 > 8，失败幅度 1；U=23/24<1 |
| `examples/unschedulable_overload.json` | fast(2,3)、slow(3,4) | slow 截止期错失；U=17/12>1 |
| `examples/invalid_deadline_gt_period.json` | (C,T,D)=(3,8,10) | 422 拒绝（D>T） |
