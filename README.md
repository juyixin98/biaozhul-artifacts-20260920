# 复系数多项式全部根数值求解后端

给定复系数多项式，返回其**全部根**以及足以判断“这些根是否真的可信”的误差证据，
而不是只给一串数字。技术栈：Python 3.12 · FastAPI · NumPy · SciPy(LAPACK) · mpmath。

设计的首要目标是防止 **“正常输入看起来正确，边界输入悄悄算错”**：
小残差不等于根准确，尤其是近重根、稀疏高阶、病态（Wilkinson 型）多项式。

---

## 目录结构（模块边界与数据/错误契约）

```
polyroots/
  validation.py  数值输入边界：类型/有限性校验、零首项剥离、首一化、零多项式拒绝
  models.py      跨模块数据契约：SolveOptions / RootRecord / RootEvidence /
                 FactorError / VietaCheck / SolveResult（frozen dataclass）
  errors.py      四类可区分错误（见下），含 JSON 安全序列化
  kernels.py     计算内核：companion（伴随矩阵+ZGEEV）/ aberth（同时迭代，逐根收敛状态）
  evidence.py    误差证据：相对残差、近重根聚类、敏感性 κ、因子重构、Vieta
  ordering.py    稳定排序（实部,虚部,下标）+ 显式容差的共轭配对
  reference.py   mpmath 高精度独立参考（测试真值，不被被测内核自证）
  runlog.py      可重放运行日志 + 幂等 run_id 注册表（状态冲突检测）
  engine.py      编排：校验 → 内核 → 证据 → 排序/配对 → 状态判定 → 写日志
  config.py      默认容差/上限
api/main.py      FastAPI：仅做协议转换与 HTTP 错误码映射
tests/           独立测试（真值来自显式已知根或 mpmath，绝不调用被测内核自证）
examples/        curl 与 httpx 示例
```

模块间只通过 `models.py` 的 dataclass 传数据，通过 `errors.py` 的分类异常传错误。

---

## 四类必须可区分的失败（HTTP 映射）

| 类别 category        | code                    | HTTP | 触发情形                         |
|----------------------|-------------------------|------|----------------------------------|
| `input_error`        | `invalid_coefficients`  | 400  | 非数值、形状错、空数组           |
| `input_error`        | `non_finite_coefficient`| 400  | NaN/Inf（与一般格式错误分开）    |
| `input_error`        | `zero_polynomial`       | 400  | 全零系数（零多项式无定义，拒绝） |
| `input_error`        | `invalid_option`        | 400  | tol/迭代次数等选项非法           |
| `state_conflict`     | `run_id_conflict`       | 409  | 同一 run_id 关联了不同输入       |
| `resource_exhausted` | `resource_exhausted`    | 413  | 次数超过 `max_degree`            |
| `computation_failed` | `computation_failed`    | 422  | NaN/Inf、LAPACK 失败等           |

**迭代耗尽不是异常**：结果以 `200` + `status="not_converged"` 正常返回，
未收敛根标 `kind="unconverged"` 并保留当前近似、残差、迭代数与末轮校正量。

---

## 防“悄悄算错”的四项落实

1. **逐根残差 + 整体因子重构误差**
   - 每根返回 `relative_residual = |f(z)| / Σ|c_k||z|^k`（尺度无关）。
   - 整体由根重构系数比较输入。主判据用**包络归一化**（除以积模长之和，
     对消去稳健），同时保留 `strict_float64_error`（严格相对误差，会暴露原始
     消去）与按需触发的 `high_precision_error`（mpmath 高精度独立重构同一批根）。
   - **近重根不仅看残差**：额外给出簇间距 `cluster_separation` 与单根条件数
     指标 `sensitivity_indicator κ`。实测间距 1e-7 的近重根，残差可低至 1e-21，
     但根的真实误差只有 ~1e-8（≈ κ·ε，κ≈4e7）。系统会把这种根标为
     `near_repeated` 并产生显式警告，绝不据此声称精确。

2. **零首项规范化，零多项式拒绝**
   - 高端零系数按相对尺度剥离后再求次数（`[0,0,1,2]` → 次数 1），并首一化。
   - 全零输入直接 `zero_polynomial` 拒绝；1e-300 量级但非零的多项式不会被误杀。

3. **稳定排序 + 明确的共轭配对容差**
   - 排序键 `(实部, 虚部, 原始下标)`，结果顺序与输入排列无关。
   - 是否共轭**完全由显式 `conjugate_tol`（默认 1e-8）决定**，不做任何隐式吸附；
     全局最优贪心保证一个根不会被配两次；实根不参与配对。容差在响应中回报。

4. **迭代耗尽保留未收敛状态**
   - Aberth 逐根以“相对校正量 < tol 且相对残差 < tol”双条件判收敛。
   - 近重根/精确重根在 float64 下只能停滞在 √eps 量级 → 预算耗尽或停滞检测触发后，
     这些根保留为 `unconverged`，记录迭代数、末轮校正量/残差与判断理由。

### 一个关键取舍：稀疏高阶多项式的重构“假误差”

`x^64−1` 的根可精确到 1e-14、Vieta 关系完美，但用 float64 逐次相乘重构时，
在零系数位置发生灾难性消去，**严格重构误差高达 0.13**。本服务用三路证据区分
“消去算术假象”与“根集合真的错了”：包络归一化误差（~2e-14）、Vieta（~4e-14）、
mpmath 高精度独立重构（~6e-14），并在 `warnings` 中明确解释，而不是静默报错或
静默放过。

---

## 支持范围与限制

- 次数 1…`max_degree`（默认 256，可用环境变量 `POLYROOTS_MAX_DEGREE` 调整）。
- 系数支持实数、复数、`[实, 虚]`、`{"real":..,"imag":..}`；顺序 `descending`
  （默认，numpy.roots 约定）或 `ascending`。
- 内核：
  - `companion`：Frobenius 伴随矩阵 + SciPy LAPACK(ZGEEV)。稳定、无需初值；
    但 LAPACK 不提供逐根收敛信息，可信度由整体证据裁定。
  - `aberth`：自带 Aberth–Ehrlich 同时迭代（固定种子、可重放），逐根收敛状态，
    适合高阶并能表达“部分根未收敛”。
  - `auto`（默认）：n≤100 用 companion，否则 aberth。
- float64 数值精度：近重根/病态根的精度受条件数限制，这是数学本质，系统如实呈现
  而非掩盖。需要更高精度时可参考 `polyroots.reference`（mpmath，dps 可调）。

---

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-lock.txt   # 锁定版本，可复现
POLYROOTS_LOG_DIR=logs/runs \
  .venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
```

健康检查：`curl http://127.0.0.1:8000/health`

## 示例请求

`x^2 + 1`（降序系数 `[1,0,1]`，根 ±i）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/roots \
  -H 'Content-Type: application/json' \
  -d '{"coefficients":[1,0,1],"run_id":"ex-1"}'
```

复系数 `(1+i)x + 2`（根 = −1+i）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/roots \
  -H 'Content-Type: application/json' \
  -d '{"coefficients":[{"real":1,"imag":1},[2,0]]}'
```

更多用例（近重根、x^64−1、零多项式拒绝）：

```bash
bash examples/example_requests.sh
POLYROOTS_BASE_URL=http://127.0.0.1:8000 .venv/bin/python examples/client_example.py
```

读取历史运行（可重放）：`GET /api/v1/runs/{run_id}`。

### 响应要点

- `status`：`converged` / `not_converged`。
- `roots[].kind`：`simple` / `near_repeated` / `zero` / `unconverged`。
- 每根含 `relative_residual`、`cluster_separation`、`sensitivity_indicator`、
  `cluster_id`、`converged`、`iterations`、人可读 `note`、`conjugate_of`。
- `factor_error`：`max_rel_coeff_error`（包络，主判据）、`strict_float64_error`、
  `high_precision_error`、`cancellation_ratio`。
- `vieta`：根和/根积相对误差；`warnings`：显式判断理由；`tolerances`：生效容差。

---

## 运行测试

```bash
.venv/bin/python -m pytest                       # 全量
.venv/bin/python -m pytest -m "not integration"  # 仅快速单元测试
```

测试覆盖：输入边界与错误分类、证据算法、两内核对已知实/复根、近重根专项、
迭代耗尽、高阶稀疏 x^64−1、x^5、Wilkinson 1..15、稳定排序/共轭容差、
幂等与状态冲突、API 四类 HTTP 错误映射。

**参考答案独立性**：测试真值只来自 ① 测试侧由显式已知根独立展开的合成夹具，
② `polyroots.reference`（mpmath 高精度，独立算法），被测内核不参与生成答案。
测试断言具体数值与失败类别（如近重根误差必须落在 1e-11…1e-7 且残差比它小
三个数量级），而非“接口能调用”。

最近一次实际运行结果见 `reports/`（含通过/失败/未执行项与时间戳）。
