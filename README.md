# 鲁棒线性回归（纯后端）

用 Python + NumPy 实现的**鲁棒线性回归计算库 + JSON 接口**。核心算法（SVD 最小二乘、
Huber 损失 IRLS / MM 迭代、加权岭回归）均自行实现（线性代数底层只调用
`numpy.linalg.svd`，不调用 `np.linalg.lstsq/solve/inv`）。无前端、无 Web 框架，
输入输出均为 JSON。

- Python >= 3.10，运行时依赖仅 `numpy>=1.24`（开发验证环境：Python 3.12.3 / NumPy 2.5.3）
- 规模限定（小中规模）：`1 <= n <= 100_000`，`1 <= p <= 200`

---

## 1. 目录结构

```
.
├── robust_regression/          # 计算库
│   ├── __init__.py             # 公开 API
│   ├── linalg.py               # SVD 最小二乘 / 加权岭回归（秩亏安全）
│   ├── huber.py                # Huber IRLS、OLS 对照、目标函数
│   ├── validation.py           # 输入范围 / 容差 / 错误类型
│   ├── api.py                  # JSON 请求 -> JSON 响应（不抛异常）
│   └── cli.py                  # stdin/stdout JSON 命令行入口
├── examples/                   # 请求样例与实际运行得到的响应
├── scripts/acceptance.py       # 验收脚本（合成离群数据对照等 19 项检查）
├── tests/                      # 58 个 unittest 自动化测试
├── reports/                    # 实际运行记录（验收日志 + 机器可读 JSON）
└── requirements.txt
```

---

## 2. 快速开始

```bash
pip install -r requirements.txt

# 库调用
python -c "
from robust_regression import fit_huber, fit_ols
X = [[0.0],[1.0],[2.0],[3.0],[4.0]]
y = [1.0, 3.0, 5.0, 7.0, 30.0]
print(fit_ols(X, y)['coefficients'])
print(fit_huber(X, y, delta=0.5)['coefficients'])
"

# JSON 命令行
python -m robust_regression.cli < examples/request_huber_outlier.json
```

---

## 3. 数学模型与算法

### 3.1 Huber 损失 IRLS（MM 迭代）

最小化

```
Q(β, b) = Σ_i ρ_δ(y_i - x_iᵀβ - b) + (λ/2)‖β‖²

ρ_δ(r) = 0.5 r²                 |r| ≤ δ
         δ|r| - 0.5 δ²          |r| >  δ
```

- **截距 b 不参与岭惩罚**（`λ` 只作用于 `β`）。
- IRLS 第 k 轮取权重 `w_i = min(1, δ/|r_i^(k)|)`（实现带有 `1e-12` 权重下界），
  求解加权岭回归得到新参数。
- 这是 Huber 损失的 **MM 迭代**：加权二次代理函数在当前点相切且处处不小于
  Huber 损失，因此完整步目标值单调不增。实现另带回溯减半（最多 50 次）
  作为数值安全兜底。
- 内层加权岭回归用一次 SVD 求解（加权行 + 岭惩罚行增广后求伪逆解），不开方
  构造正规方程矩阵，数值稳定性更好。
- **初值为 OLS**（SVD 伪逆解）。

### 3.2 δ = "auto"

以 OLS 初值残差的稳健尺度估计自动选 δ：

- `δ = 1.4826 · MAD`，`MAD = median|r - median(r)|`（正态残差下与标准差一致）；
- MAD 低于零散布容差 `1e-12 · max(1, ‖y‖∞)`（用于吸收 SVD 舍入噪声）时：
  - 所有残差都低于该容差 → **全零残差/完美拟合**，δ=1（取值已不影响结果），
    响应带 `auto_scale_all_zero_residuals` warning；
  - 否则回退到 `δ = 1.349 · std(r)`，带 `auto_scale_zero_spread_fallback_residual_std`。

已知限制（文档化行为）：MAD 基于 OLS 初值。当**高杠杆单侧污染**把 OLS 回归线
整体拉偏时，残差 MAD 可能偏大，auto δ 也随之偏大。对污染机制已知的数据，
建议显式传入 δ（例如噪声标准差的 1～1.5 倍）。测试 `test_auto_delta_on_contaminated_high_leverage_is_stable`
覆盖这种情形：结果仍保证收敛且目标不增。

### 3.3 秩亏处理（显式上报，不静默）

对增广设计矩阵（含截距列）做紧 SVD，以 `rcond = 1e-10`（相对最大奇异值）判定
有效秩：

- 秩亏时返回 **Moore–Penrose 最小范数最小二乘解**（解确定、可复现），
  并在响应中上报 `rank`、`rank_deficient: true` 与 warning
  `rank_deficient_min_norm_solution`；
- 岭系统的秩诊断基于**未加惩罚行**的加权增广矩阵（否则 λ>0 时惩罚行会让
  系统恒满秩，掩盖 X 的共线性），见 `linalg.solve_weighted_ridge`。

### 3.4 OLS 对照

`method="ols"` 走同一个 SVD 求解器，返回系数、截距、SSE、秩与秩亏标志。
δ → +∞ 时 Huber 退化为 OLS（验收中 δ=1e6 二者差异为 0，见验收报告）。

---

## 4. 输入范围、容差与失败状态

### 4.1 输入范围（`validation.py`，超出即 `input_error`）

| 项 | 范围 / 要求 |
|---|---|
| `X` | 二维数组，`1 ≤ n ≤ 100_000`，`1 ≤ p ≤ 200` |
| `y` | 一维数组，长度等于 n |
| 元素值 | 必须有限（拒绝 NaN/Inf），且 `|x_ij|, |y_i| ≤ 1e8`（请先缩放数据） |
| `delta` | `"auto"` 或 `[1e-12, 1e12]` 内正数 |
| `reg_lambda` | `[0, 1e12]` |
| `tol` | `[1e-14, 1.0]`，默认 `1e-7`（同时要求系数相对变化与目标相对变化 ≤ tol） |
| `max_iter` | `[1, 10_000]` 整数，默认 100 |
| `fit_intercept` | 布尔，默认 true |

布尔值不会被当作 0/1 数值接受（`delta: true` 等会报错）；参差不齐的二维
数组、字符串元素均被拒绝。

### 4.2 数值容差

| 常量 | 值 | 含义 |
|---|---|---|
| `RCOND` | `1e-10` | SVD 有效奇异值 = `s_i > s_max · rcond` |
| `WEIGHT_FLOOR` | `1e-12` | IRLS 权重下界，防除零 |
| `MONOTONIC_SLACK` | `1e-12` | 单调下降判定允许的相对松弛（容纳 SVD 舍入） |
| 零散布容差 | `1e-12·max(1,‖y‖∞)` | auto δ 下判定 MAD/残差为零散布 |
| 收敛判据 | `tol` | `‖β_new−β‖/max(1,‖β‖) ≤ tol` 且目标相对变化 ≤ tol |

### 4.3 失败 / 终止状态（`result.status`）

| status | 含义 |
|---|---|
| `converged` | 在 tol 下收敛（`converged: true`） |
| `max_iterations_reached` | 达到 max_iter 仍未收敛；**结果照常返回**（MM 迭代保证它不劣于初值） |
| `objective_non_decreasing` | 回溯 50 次仍找不到下降步（极罕见的病态情形） |

JSON 层错误（`ok: false`）：

| error.type | 含义 | CLI 退出码 |
|---|---|---|
| `input_error` | 请求不合法（形状/范围/非有限值/JSON 语法错误） | 语法错误 2，语义错误 1 |
| `numerical_error` | 输入合法但数值计算无法继续（如解出 NaN/Inf） | 1 |

成功时 CLI 退出码 0。

---

## 5. JSON 接口

请求：

```json
{
  "method": "huber",
  "X": [[0.0], [1.0], [2.0]],
  "y": [1.0, 3.0, 5.0],
  "delta": "auto",
  "reg_lambda": 0.0,
  "tol": 1e-7,
  "max_iter": 100,
  "fit_intercept": true
}
```

`method` 为 `"huber"`（默认）或 `"ols"`；ols 忽略 Huber 专属参数。

成功响应（以下数值为示意；真实样例响应见 `examples/response_*.json`）：

```json
{
  "ok": true,
  "method": "huber",
  "result": {
    "coefficients": [2.0],
    "intercept": 1.0,
    "delta_used": 0.4478,
    "residual_scale": 0.4478,
    "reg_lambda": 0.0,
    "tol": 1e-7,
    "max_iter": 100,
    "fit_intercept": true,
    "iterations": 7,
    "converged": true,
    "status": "converged",
    "objective": 0.0021,
    "objective_history": [0.01, 0.003, 0.0021],
    "rank": 2,
    "rank_deficient": false,
    "warnings": ["auto_scale_from_mad"]
  }
}
```

失败响应：

```json
{"ok": false, "method": "huber",
 "error": {"type": "input_error", "message": "..."}}
```

所有响应保证是合法 JSON（序列化使用 `allow_nan=False`，不出现
`NaN`/`Infinity` token）。完整字段以 `tests/test_api.py` 的契约测试为准。

### 在代码中使用 JSON 接口

```python
from robust_regression import fit_from_json
response = fit_from_json(request_dict)   # 永不抛异常，错误体现在 response
```

---

## 6. 请求样例（`examples/`）

| 文件 | 说明 |
|---|---|
| `request_huber_outlier.json` | 30 点（中心化 x）含 4 个 ±8 中部垂直离群点，δ=0.5 |
| `request_ols_outlier.json` | 同一数据的 OLS 对照 |
| `request_huber_collinear.json` | 三列严格共线（`x, x, 2x`），秩亏 |
| `request_huber_zero_residual.json` | 严格线性关系、无截距，全零残差 + δ="auto" |
| `request_error_shape_mismatch.json` | 故意构造的 X/y 长度不一致错误 |
| `response_*.json` | 上述请求实际运行得到的响应（已随仓库保存） |

---

## 7. 测试与验收（实际运行）

### 7.1 自动化测试

```bash
python -m unittest discover -s tests -v
```

58 个测试，分布：

- `tests/test_linalg.py`：SVD 最小范数解、秩亏、全零矩阵、加权岭回归正规方程、
  截距不受岭惩罚、带岭惩罚时秩诊断仍能识别共线性；
- `tests/test_huber.py`：收敛与目标单调不增、δ→∞ 等价 OLS、离群点对比 OLS、
  可复现（逐位一致）、共线列、全零列、全零残差、无截距、岭收缩、单样本、
  auto δ 两条路径；
- `tests/test_validation.py`：形状/有限性/幅值/规模/各超参范围/布尔不冒充数值；
- `tests/test_api.py`：JSON 契约、可序列化（无 NaN token）、默认值、各错误类型；
- `tests/test_cli.py`：stdin/stdout 端到端、退出码、两进程输出一致。

### 7.2 验收脚本

```bash
python scripts/acceptance.py --json reports/acceptance_report.json
```

19 项检查，覆盖任务要求的四个方面（结果见 `reports/acceptance_run.log`，
机器可读结果见 `reports/acceptance_report.json`）：

1. **合成离群数据对照 OLS**：Huber 斜率 1.9834（真值 2，误差 0.017）、
   截距 1.0012（误差 0.001）；OLS 斜率被拉偏到 1.8334；离群点平均权重 0.062，
   干净点 0.987；目标由 OLS 初值 18.468 下降到 16.331，9 轮收敛，轨迹零上升；
2. **共线列**：OLS 与 Huber 均上报 `rank=2 / rank_deficient=true`，
   带 `rank_deficient_min_norm_solution` warning，预测最大误差 3.6e-15；
3. **全零残差**：δ="auto" 走零散布路径（`auto_scale_all_zero_residuals`），
   目标 1.9e-29（机器精度），1 轮收敛，参数精确恢复；
4. **可复现性**：两次拟合逐位一致；δ=1e6 时 Huber 与 OLS 系数/截距差为 0；
   JSON 接口与库调用目标一致，非法请求返回 `input_error`。

---

## 8. 设计取舍说明

- **只做 SVD 不做 QR/Cholesky**：规模限定小中规模，SVD 在秩亏下直接给出
  最小范数解并顺带给出秩诊断，一套求解器同时服务 OLS、IRLS 内层和诊断。
- **MM 迭代 + 回溯**：理论保证目标不增，回溯只用于抵御舍入误差；
  每次 IRLS 内层用“加权 + 岭惩罚行增广”的一次 SVD，无需显式求逆。
- **δ 的语义**：与 y 同量纲。δ 越小越接近 L1（越拒绝离群点），δ→∞ 退化为 OLS。
- **可复现性**：算法确定性，无随机初始化；合成数据的随机数生成器使用固定种子。
