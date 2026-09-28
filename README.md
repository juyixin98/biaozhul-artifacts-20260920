# krylov-expv

稀疏矩阵指数作用服务：计算 `w = exp(t*A) @ v`，使用 Krylov 子空间近似，
**从不形成完整的稠密矩阵指数**。A 始终保持稀疏（仅以矩阵-向量乘积形式
使用）；只有投影得到的小 Hessenberg 矩阵（≤ m_max）被稠密指数化。

## 模块划分

| 模块 | 职责 |
|---|---|
| `krylov_expv/inputs.py` | 数值输入边界：COO 校验、装配 CSR、标量/容差校验 |
| `krylov_expv/core/arnoldi.py` | Arnoldi 迭代（一次重正交化，happy breakdown 检测） |
| `krylov_expv/core/dense_expm.py` | 小矩阵增广指数（一次 expm 同时给出步进向量与 phi_1 量） |
| `krylov_expv/core/estimator.py` | **子空间残差**与**误差估计**两个不同量的计算 |
| `krylov_expv/core/integrator.py` | 时间分段、重启、容差分配、非收敛判定 |
| `krylov_expv/evidence.py` | 误差证据记录（每步 τ、维数、残差、估计） |
| `krylov_expv/service/` | FastAPI 接口、请求关联、结构化失败响应 |
| `krylov_expv/config.py` | 全部数值策略参数（分段/重启规则固定于此） |

## 数值规则（固定，无隐藏自适应）

- 每个时间步重建一次 Krylov 基（每步即一次重启），维数 ≤ `m_max`（默认 30）。
- 候选步长 τ 按固定因子 0.5 折半，直到误差估计 ≤ `acceptance_safety(0.25)
  × tol × ‖w‖ × |τ| / |t|`；折半上限 `max_halvings`(60)，超出报
  `step_size_underflow`。
- 接受步的估计低于局部容差 10% 时下一步长按因子 2 增长。
- 接受步数上限 `max_steps`(512)，超出返回 **not_converged**（类别
  `max_steps_exceeded`），并返回当前最优部分解与完整证据，绝不静默。
- 子空间残差 `β·h_{k+1,k}·|s_k|`（ODE 残差）与误差估计
  `β·h_{k+1,k}·|τ·phi_1(τH)_{k,1}|`（Expokit 式主导项）分别计算、分别上报。
- 退化情形显式处理：`t == 0` 返回 v 的副本（`termination=t_zero`）；
  零向量返回零（`termination=zero_vector`）；`t < 0` 走同一积分路径
  （τ 带符号，分段轨迹之和等于 t）。
- 规模预算包含基向量存储：`(m_max+1)·n·8 + (m_max+1)·m_max·8 + 4·n·8`
  字节，超出预算在任何计算之前拒绝（类别 `memory_budget_exceeded`）。

## 复现

```bash
pip install -r requirements.txt        # 锁定版本
python3 scripts/make_fixtures.py       # 重新生成 fixtures/matrices/*.json
python3 -m pytest                      # 31 个测试（含 mpmath 高精度对照）
python3 -m pytest --cov=krylov_expv    # 覆盖率（当前 97%）
```

## 服务调用

```bash
uvicorn krylov_expv.service.app:app --port 8000
python3 examples/client_example.py     # 正常 + 异常各一次调用
```

或直接用 curl：

```bash
curl -s localhost:8000/v1/expv -H 'content-type: application/json' -d '{
  "request_id": "demo-1",
  "matrix": {"n": 3, "row": [0,1,2], "col": [0,1,2], "data": [-1.0,-0.5,0.25]},
  "vector": [1.0, -2.0, 0.5],
  "t": 1.25, "tol": 1e-12}'
```

响应包含：`request_id`（客户端提供或自动生成，同时出现在响应头与日志）、
`status`（converged / not_converged / rejected）、`w`、
`total_error_estimate`、`max_subspace_residual`、逐步 `steps` 证据、
`versions`（krylov_expv/numpy/scipy）、`memory_bytes_used`、`elapsed_ms`，
以及失败时的 `failure: {category, message}`。

失败类别：`invalid_input`（HTTP 400）、`memory_budget_exceeded`（413）、
`max_steps_exceeded` / `step_size_underflow`（200 + `not_converged`）。

配置可用环境变量覆盖：`KRYLOV_EXPV_M_MAX`、`KRYLOV_EXPV_TOL`、
`KRYLOV_EXPV_MAX_STEPS`、`KRYLOV_EXPV_MAX_HALVINGS`、
`KRYLOV_EXPV_MEMORY_BUDGET_BYTES`；单次请求可用 `m_max`、`max_steps`、
`tol` 字段覆盖。

## 验证设计

- 参考答案由 `tests/reference.py` 用 **mpmath 50 位精度 `expm`** 独立生成，
  不由被测内核产生。
- 夹具（`fixtures/matrices/`，全部由 `scripts/make_fixtures.py` 确定性生成）：
  - `diag3` 对角阵（闭式解对照）；
  - `jordan4` 4×4 Jordan 块（亏损矩阵，验证 happy breakdown 精确性）；
  - `nonnormal8` Grcar 型强非正规矩阵（瞬态增长）；
  - `advection60_long` 反对称平流算子 n=60、t=120（虚谱无衰减，强制
    时间分段与重启）；
  - `rotation3` 旋转生成元（用于 t < 0）。
- 测试断言具体数值与失败类别：分段轨迹是 [0,t] 的完整划分、分段组合
  exp(t1)·exp(t2) 与整体一致、误差预算是逐步估计之和且受容差约束、
  非收敛/超预算/欠流各自产生规定类别。
- 真实运行留痕：`docs/smoke_output.txt` 保存了一次实服务冒烟输出
  （正常、非法输入、不收敛三种路径）。

## 已知边界

- 误差估计为主导项估计（Expokit 风格），接受规则用固定安全系数 0.25
  补偿其系统性偏小；它是"估计"而非严格上界。
- 仅支持实数双精度；复矩阵未覆盖。
- 单进程内存预算，无分布式。
