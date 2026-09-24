# 有界模型预测控制（Bounded MPC）后端 — 双积分器

纯后端、无硬件依赖的离散时间线性 MPC 服务：输入当前状态与参考轨迹，
用固定预测窗最小化**跟踪误差与控制变化**，并对**速度与加速度**施加硬约束。
QP 由 [OSQP](https://osqp.org/) 求解，服务框架为 [FastAPI](https://fastapi.tiangolo.com/)。

- **模型**：1-D 双积分器（单位质点），状态 `x=[p, v]`，控制 `u = 加速度`。
- **离散步**：精确零阶保持，`dt` 可配置：
  - `A = [[1, dt],[0, 1]]`，`B = [[dt²/2],[dt]]`，`xₖ₊₁ = A xₖ + B (uₖ + dₖ)`
  - 所有 QP 矩阵均由该模型与配置权重/限值构建（见 `app/mpc.py::_build_matrices`）。
- **代价**：`Σ (xₖ-rₖ)ᵀQ(xₖ-rₖ) + x_NᵀQf x_N + Σ uₖᵀRu uₖ + Σ ΔuₖᵀRdu Δuₖ`
  - `Qf` 由离散代数 Riccati 方程（DARE）解出；`Δu₀` 以**当前实际施加**的 `u_prev` 为基准。
- **硬约束**：预测窗内速度 `|v| ≤ v_max`、加速度 `|u| ≤ u_max`，动力学等式。
- **合成扰动**：确定性、可重复、有界（`|dₖ| ≤ amplitude`），仅进入被控对象，
  控制器不可见，用于检验鲁棒性。不接硬件，时间步完全可控。

## 求解失败时的保守回退（不输出未验证控制）

每次求解后，控制量必须通过**独立的残差校验**（动力学、速度、加速度）才允许返回。
下列情况一律返回**保守回退**：`u_fb = clip(-v/dt, ±u_max)`（一个采样周期内刹停的制动量），
并携带明确状态码与文字原因，响应 HTTP 200（失败通过 `status/fallback/reason` 表达）：

| status | 触发条件 |
|---|---|
| `solved` | OSQP `solved` 且残差校验通过（唯一可信的最优解） |
| `infeasible_initial_state` | 初速度本身超出 `v_max`（上线前预先判定） |
| `solver_infeasible` | OSQP 返回 `primal/dual infeasible` |
| `solver_timeout` | OSQP 在 `time_limit` 内未收敛 |
| `solver_error` | 求解器抛异常 |
| `solver_incomplete` | 其他非最优终止状态 |
| `verification_failed` | 解已返回但独立残差校验不通过 |

回退时 `predicted_states/predicted_controls` 为空——**绝不复用上次的未验证解**。

## 目录结构

```
app/
  model.py        # 双积分器 ZOH 模型（A、B 由模型导出）
  disturbance.py  # 确定性有界合成扰动
  mpc.py          # QP 构建（模型导出）、OSQP 求解、独立校验、保守回退
  simulation.py   # 确定性闭环仿真（可控时间步）+ 逐步状态一致性核验
  schemas.py      # FastAPI/Pydantic 请求校验（拒绝 NaN/Inf、越界配置）
  api.py          # HTTP 接口
examples/         # 示例请求
tests/            # 31 个自动化测试
requirements.txt  # 直接依赖（版本区间）
requirements.lock # 已解析、完全锁定的依赖（验收安装用它）
```

## 本地启动

需要 Python 3.11+（开发环境为 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P069/a
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock      # 锁定依赖，可复现安装
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

## 接口

- `GET  /health` — 服务与 OSQP 版本
- `GET  /matrices` — 导出模型/权重/约束矩阵（审计用）
- `POST /solve` — 单步 MPC
  ```json
  {"state": [1.5, 0.0], "reference": [0.0], "u_prev": 0.0,
   "config": {"horizon": 20, "dt": 0.1, "v_max": 2.0, "u_max": 1.0}}
  ```
  `reference` 长度可小于预测窗，末端自动保持；`config` 可整体省略取默认值。
- `POST /simulate` — 确定性闭环仿真
  ```json
  {"initial_state": [0.0, 0.0], "reference": [0,0,2,2],
   "steps": 60, "disturbance_amplitude": 0.2, "disturbance_seed": 1}
  ```
  返回逐步记录：施加前后状态、控制量、扰动量、回退标志/原因、
  速度与输入越界量、求解残差。

### curl 示例

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' -d @examples/solve_brake.json
# 不可行初态：返回 fallback=true 的保守制动
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' -d @examples/solve_infeasible_initial.json
curl -s -X POST http://127.0.0.1:8000/simulate \
  -H 'Content-Type: application/json' -d @examples/simulate_step_change.json
```

## 验收命令

```bash
# 1) 全量自动化测试（模型/约束激活/参考突变/不可行初态/超时/求解器异常/校验失败/HTTP）
pytest -q

# 2) 启动服务（另开一个终端）
uvicorn app.api:app --host 127.0.0.1 --port 8000

# 3) 端到端冒烟
curl -s http://127.0.0.1:8000/health | python -m json.tool
curl -s -X POST http://127.0.0.1:8000/solve \
  -H 'Content-Type: application/json' -d @examples/solve_brake.json | python -m json.tool
```

## 测试覆盖（31 个）

- **模型与扰动**：ZOH 矩阵解析值、状态更新闭式解、扰动经 `B` 进入且严格有界、确定性。
- **QP 构建来自模型**：约束矩阵维度、Hessian 正定、`Qf` 满足 DARE 不动点。
- **约束激活**：高速时首拍打满 `-u_max`；贴 `v_max` 起跑时全程不破速度限且限值激活。
- **逐步核验**：预测状态逐步满足 `xₖ₊₁ = A xₖ + B uₖ`；闭环每一步用模型重推状态。
- **参考突变**：0→2 阶跃后跟踪到新设定值，全程速度/加速度不破限。
- **不可行初态**：`|v₀| > v_max` 返回保守制动与原因，且持续制动至可行后恢复 MPC。
- **求解超时**：注入 `run time limit reached` → `solver_timeout` + 回退；
  另含 1e-12 s 时间预算的真实 OSQP 超时尝试（机器太快时至少保证解是校验过的）。
- **求解失败**：注入求解器异常/不可行状态 → 对应状态码 + 回退；
  校验残差被破坏 → `verification_failed`。
- **不输出陈旧控制**：先正常求解再令其超时，断言失败步输出的是新鲜回退制动而非上次最优解。
- **HTTP**：健康检查、矩阵导出、配置覆盖、非法/NaN 输入返回 422、仿真端到端。

## 设计约定与诚实声明

- 求解器每次调用新建实例，无跨请求状态；所有数值路径真实由 OSQP 执行，未模拟结果。
- 残差校验独立于求解器重写动力学递推；任何一项超过 `verify_tol` 即拒绝该解。
- 本仓库只做后端计算与 JSON API，不含任何前端页面或硬件接口。
