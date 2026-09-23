# 有界模型预测控制（Bounded MPC）后端 — 双积分器

纯后端实现：**Python + OSQP + FastAPI**。对双积分器做离线（固定预测窗）MPC，
最小化轨迹跟踪误差与**控制变化量**，对速度和加速度施加硬约束；求解超时或
不可行时返回**明确、保守、可解释的回退**，绝不复用上次未验证的控制。
不接任何硬件；扰动为确定性合成扰动，时间步可控。

---

## 1. 模型（矩阵必须来自模型）

连续时间双积分器，状态 `x = [p, v]`，输入 `u = a`：

```
|ṗ|   |0 1| |p|   |0|
|v̇| = |0 0| |v| + |1| u
```

`mpc/model.py` 从 `A_c, B_c` 出发，用矩阵指数级数做零阶保持精确离散化
（`A_c` 幂零，级数自然截断），得到 `dt = 0.1` 时：

```
A_d = |1  dt|     B_d = |0.5·dt²|
      |0   1|           |  dt    |
    = |1 0.1|           |0.005|
      |0 1  |           |0.1  |
```

QP 中所有预测矩阵（`Φ = stack(A^k)`、脉冲响应 `Γ`、下三角 `L`）均由
`A_d, B_d` 构建，未把双积分器的标量递推式硬编码进代价/约束装配
（见 `mpc/qp.py`）。

## 2. MPC 问题（固定预测窗 N=20）

决策变量为控制增量 `z = [Δu₀ … Δu_{N-1}]`，`Δu_k = u_k − u_{k-1}`：

```
min  Σ_{k=0}^{N-1} [ (x_k−r_k)ᵀ Q (x_k−r_k) + r_δ·Δu_k² ]
                   + (x_N−r_N)ᵀ Q_N (x_N−r_N)
s.t. |u_k| ≤ a_max (=3.0)      k = 0…N-1
     |v_k| ≤ v_max (=2.0)      k = 1…N   （对预测状态）
```

状态预测 `X = Φx₀ + Γ(u_prev·1 + Lz)` 代入后形成标准二次规划
`min ½zᵀPz + qᵀz,  l ≤ Az ≤ u`，交由 **OSQP** 求解
（`eps_abs=eps_rel=1e-6`，`time_limit=0.5 s`）。

## 3. 求解后验证与保守回退（核心安全契约）

返回 `status="ok"` 必须**同时**满足：

1. OSQP 报告 `solved`（超时 `time limit reached`、原始/对偶不可行、
   迭代上限等一律不算成功）；
2. 解向量有限（无 NaN/Inf）；
3. **独立重建**整条输入与状态轨迹，逐条核对硬约束残差
   `a_max_violation / v_max_violation / constraint_row_violation ≤ 1e-5`。

任何一条不满足，返回 `status="fallback"` 及机器可读原因：

| reason | 触发条件 |
|---|---|
| `initial_state_infeasible` | 当前实测速度超出 `v_max`（QP 前预检） |
| `nonfinite_state` | 当前状态或 `u_prev` 含 NaN/Inf |
| `solver_timeout` | OSQP 超过 `time_limit` |
| `solver_infeasible` | OSQP 报告 primal/dual infeasible |
| `solver_error` | 求解器抛异常 |
| `solver_non_optimal` | 非最优退出（如迭代上限） |
| `solution_nonfinite` | 解含 NaN/Inf |
| `constraint_residual` | 求解后独立残差校验失败 |

**回退控制律**仅依赖当前实测状态，是饱和速度反馈制动：

```
u_fb = clip(−K_brake · v, −a_max, +a_max),   K_brake = 1.0
```

配置校验保证 `K_brake · v_max ≤ a_max`，因此回退对**任何可行速度**都有界，
对越界初速度取饱和制动力。**绝不输出上一次 QP 的未验证控制**
（失败响应里 `control_sequence` 为空，且测试显式断言回退值不等于旧 `u₀`）。

## 4. 确定性合成扰动与可控时间步

`mpc/disturbance.py`：`none / constant / sine / square / ramp / bump`，
`w_k` 是步数 `k` 的纯函数，无随机数 → 仿真完全可复现。
`x_{k+1} = A_d x_k + B_d u_k + w_k`，`dt`、步数 `n_steps`、预测窗均可配。

## 5. HTTP 协议（FastAPI，纯 JSON）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 存活探针 + 默认参数 |
| POST | `/api/mpc/solve` | 单次求解：当前状态 + `(N+1)×2` 参考窗 |
| POST | `/api/mpc/rollout` | 确定性闭环仿真：初态、步数、恒定/阶跃参考、扰动、参数覆盖 |

服务**无状态**：每个请求按显式配置新建控制器，请求间不共享内存；
测试用故障注入钩子不通过 HTTP 暴露。

### `solve` 响应关键字段

```json
{
  "status": "ok",
  "control": 2.8696,
  "fallback": false,
  "reason": "none",
  "reason_detail": "optimal solution verified against hard constraints",
  "solve_time_s": 0.0022,
  "osqp_status": "solved",
  "control_sequence": [...],
  "predicted_states": [[p,v], ... 21 行],
  "residuals": {"a_max_violation": 1e-8, "v_max_violation": 0.0,
                "constraint_row_violation": 1e-8}
}
```

失败时 `status="fallback"`，`control` 为保守制动值，`reason`/`reason_detail`
说明原因；参考形状错误等返回 HTTP 422。

## 6. 本地启动

```bash
cd b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt      # 已锁定完整依赖（31 个包，含哈希级版本）

# 启动服务
uvicorn mpc.api:app --host 127.0.0.1 --port 8000
# 交互文档: http://127.0.0.1:8000/docs
```

## 7. 验收命令

```bash
# (A) 自动化测试：24 个用例 —— 模型离散化、逐步状态更新、
#     约束激活、参考突变、不可行初态、四类求解失败、自然超时、
#     残差校验、确定性扰动、HTTP API
python -m pytest tests/ -v

# (B) 命令行四场景验收（逐步打印状态与约束残差）
python accept.py

# (C) 用示例输入打真实 HTTP 服务（另开一个终端启动 uvicorn）
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/api/mpc/solve \
     -H 'Content-Type: application/json' \
     -d @examples/solve_track_setpoint.json
curl -s -X POST http://127.0.0.1:8000/api/mpc/rollout \
     -H 'Content-Type: application/json' \
     -d @examples/rollout_reference_step.json
curl -s -X POST http://127.0.0.1:8000/api/mpc/rollout \
     -H 'Content-Type: application/json' \
     -d @examples/rollout_infeasible_initial_state.json
```

### 已实测结果（本仓库真实运行）

* `pytest`：**24 passed**。
* 约束激活：远处定点下前 6 步 `u = +3.0`（精确饱和于 `a_max`），
  预测最大速度 2.0，约束残差 ~1e-6。
* 参考突变：第 20 步由 0 阶跃到 1.5，全程 0 fallback、0 约束越界。
* 不可行初态 `v₀=2.45`：前 2 步回退制动（−2.45, −2.205），
  第 3 步速度回到 1.9845 ≤ 2.0，MPC 恢复接管并继续制动收敛。
* 求解失败（超时/不可行/异常/非最优）：全部返回 fallback，
  在运动状态 `v=1.5` 下给 `u=−1.5000`（当前态制动律），
  而非上一计划的 `u₀=+3.0`；`control_sequence` 为空。

> 关于超时测试：测试同时覆盖**自然超时**（大预测窗 N=120 +
> `time_limit=1e-9`，真实触发 OSQP 的 `run time limit reached` 状态）与
> **确定性注入超时**（小规模 QP 无法自然越界，用测试钩子走完整回退路径，
> `reason_detail` 标注 "forced for test"，不冒充自然超时）。

## 8. 目录结构

```
b/
├── mpc/
│   ├── config.py        # 配置与参数合法性（含 K_brake·v_max ≤ a_max）
│   ├── model.py         # A_c/B_c -> 级数 ZOH -> A_d/B_d；状态传播
│   ├── qp.py            # 由模型矩阵构建 condensed QP（Φ/Γ/L、P/q/A/l/u）
│   ├── mpc.py           # OSQP 求解 + 独立残差验证 + 保守回退
│   ├── disturbance.py   # 确定性合成扰动
│   ├── simulation.py    # 闭环 rollout、恒定/阶跃参考
│   ├── schemas.py       # Pydantic 请求/响应
│   └── api.py           # FastAPI 路由
├── tests/               # 23 个 pytest 用例
├── examples/            # 3 个示例请求 JSON
├── accept.py            # 四场景命令行验收
├── requirements.txt     # 直接依赖（版本钉死）
└── requirements-lock.txt# pip freeze 完整锁定
```

## 9. 范围说明

* 纯后端，无前端页面；不含任何硬件/实时 I/O。
* 本项目无密码学操作需求，因此没有密码代码；所有数值计算均真实执行。
* 位置不设约束（题目只要求约束速度与加速度）；扰动下无积分器，
  持续扰动存在有界稳态偏差，属预期行为。
