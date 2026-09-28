# krylov-expm-service

稀疏矩阵 `exp(tA)v` 计算服务。使用 Krylov 子空间近似（Arnoldi），**从不形成完整的
矩阵指数**；完整指数只出现在维度 ≤ `max_krylov_dim` 的小型 Hessenberg 矩阵上。

## 模块划分

```
src/krylov_expm/
  models.py          请求/响应模式（pydantic）
  validation.py      数值输入：COO 校验、CSR 归一化、t=0 / 负 t / 零向量边界处理
  budget.py          规模预算：基向量存储 (m+1)*n*8 字节 + Hessenberg 存储
  kernel.py          计算内核：Arnoldi（带完全重正交化）+ 小矩阵 expm 单步
  error_estimate.py  误差证据：子空间残差范数（精确）与积分型误差估计（区分）
  propagator.py      时间分段与重启编排（固定规则）
  evidence.py        分段/全局误差证据记录
  service.py         FastAPI 接口：请求身份关联、结构化日志、失败分类
tests/               独立测试（含 mpmath 高精度参考实现 tests/reference.py）
fixtures/            合成数据夹具（生成器 + .npz + manifest.json）
examples/            服务调用示例
results/             可复核的测试与冒烟运行记录
```

## 算法与固定规则

- **单步**：Arnoldi 构造 `K_m(A, v)`，`w = β·V_m·exp(t·H_m)·e1`，其中 `exp(t·H_m)`
  用 `scipy.linalg.expm` 在 ≤30 维稠密小矩阵上计算。Happy breakdown（Krylov 子空间
  A-不变）时结果精确，残差恰为 0。
- **子空间残差 vs 误差估计**（两个不同量，分别上报）：
  - `subspace_residual_norm`：ODE 残差的*精确*范数
    `β·h_{m+1,m}·|e_m^T exp(tH_m) e1|`（残差是秩一的）。
  - `error_estimate`：由残差积分得到的界式估计
    `∫₀ᵗ ‖r(τ)‖ dτ`，在被约化的小 Hessenberg 系统上用梯形求积计算。
- **时间分段（固定）**：段数 `= ceil(|t|·‖A‖₁ / segment_theta)`，截断到
  `[1, max_segments]`，默认 `segment_theta = 1.0`。
- **重启（固定）**：段内误差估计超过 `tol·max(1, ‖w‖)` 时步长减半重试，最多
  `max_restart_splits`（默认 6）次；仍不满足则请求标记为 **NOT_CONVERGED**，
  并在 `failure.details` 中给出当时的误差估计、Krylov 维数与步长。
- **边界情形**：`t = 0` 原样返回 `v`；零向量返回零向量（两者标记
  `trivial_case`，不做 Krylov 迭代）；`t < 0` 为合法的反向传播，分段按 `|t|` 计。
- **规模预算**：基向量存储 `(m+1)·n·8` 字节 + Hessenberg `(m+1)·m·8` 字节，
  超过 `max_basis_bytes`（默认 256 MiB）或维度超过 `max_dimension` 即拒绝，
  失败类别 `BUDGET_EXCEEDED`，details 中含估算字节数。

## 接口

- `GET /healthz` → `{"status": "ok", "version": ...}`
- `POST /v1/expmv`，请求体：

```json
{
  "matrix": {"shape": [n, n], "row": [...], "col": [...], "data": [...]},
  "vector": [...],
  "t": 1.3,
  "tol": 1e-9
}
```

响应统一信封：`request_id`、`status`（`converged` / `not_converged` / `rejected`）、
`vector`、`evidence`（分段误差证据）、`failure`（失败类别与原因，单列）、
`meta`（版本、配置快照、耗时）。HTTP 状态码：`VALIDATION_ERROR` → 422，
`BUDGET_EXCEEDED` → 413，`NOT_CONVERGED` → 200（结果不可用但属正常数值结论），
意外错误 → 500（`INTERNAL_ERROR`）。

请求身份：发送 `X-Request-ID` 头即在响应头、响应体和日志中关联；缺省时服务生成
UUID。日志每行带 `[request_id=...]`，记录请求参数、预算检查、分段数、累计误差
估计与失败原因。

## 复现

```bash
# 1. 安装依赖（锁定版本见 requirements-lock.txt）
pip install -r requirements.txt        # 或 pip install -r requirements-lock.txt

# 2. 重新生成合成夹具（确定性，固定种子 20260927）
python fixtures/generate_fixtures.py

# 3. 运行测试（37 个用例）
python -m pytest tests/ -q

# 4. 启动服务
PYTHONPATH=src uvicorn krylov_expm.service:app --port 8000

# 5. 调用示例
python examples/call_service.py
# 或 curl：
curl -X POST http://127.0.0.1:8000/v1/expmv \
  -H 'Content-Type: application/json' -H 'X-Request-ID: demo-1' \
  -d '{"matrix": {"shape": [2,2], "row": [0,1], "col": [0,1], "data": [1.0,-1.0]},
       "vector": [1.0, 2.0], "t": 0.5}'
```

服务参数可用环境变量覆盖（前缀 `KRYLOV_EXPM_`），例如
`KRYLOV_EXPM_MAX_KRYLOV_DIM=40`、`KRYLOV_EXPM_MAX_BASIS_BYTES=...`。

## 验证策略

- **独立参考答案**：`tests/reference.py` 用 mpmath（60 位精度）以 scaling-and-squaring
  + Taylor 独立实现 `exp(tA)v`，不由被测内核生成。
- **覆盖**：对称正定矩阵、Grcar 非正规矩阵、Jordan 块（亏损矩阵）、长时间步
  （t=25，强制分段）、负 t。
- **分段一致性**：同一问题在 1 段与多段划分下结果相对差 < 1e-8；累计误差估计
  等于各段估计之和；分段计划规则本身有断言。
- **失败类别**：测试断言具体类别（`VALIDATION_ERROR` / `BUDGET_EXCEEDED` /
  `NOT_CONVERGED`）及 details 内容，而非只检查接口可调用。
- **真实运行记录**：`results/test-run.txt`（pytest 输出）、
  `results/service-smoke.txt`（正常/未收敛/校验失败/t=0 四类真实 HTTP 调用）、
  `results/server.log`（带 request_id 的服务日志）。

## 数据夹具

全部本地合成、确定性生成（`fixtures/manifest.json` 有描述）：

| 夹具 | 说明 |
|---|---|
| `symmetric_tridiag` | 对称正定 1D Laplacian，n=8 |
| `grcar_nonnormal` | Grcar 非正规 Toeplitz 矩阵，n=10 |
| `jordan_block` | 特征值 0.5 的单个 Jordan 块，n=8 |
| `decay_longtime` | 负 Laplacian 衰减半群，n=12，用于 t=25 长时间测试 |
