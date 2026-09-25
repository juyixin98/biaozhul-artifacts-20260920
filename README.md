# kfmu —— 支持部分观测缺失的线性卡尔曼滤波（纯后端）

用 Python + NumPy 实现的线性卡尔曼滤波计算库。核心算法自行实现（不依赖
`filterpy` 等滤波库），仅依赖 NumPy；提供 Python 库接口和 JSON 命令行接口，
**无前端**。面向小中规模问题（状态/量测维数上限见下表）。

## 功能

- 线性离散卡尔曼滤波：预测（时间更新）与量测更新；可选控制输入 `B u`。
- **部分观测缺失**：量测分量级缺测（NaN/`null` 或布尔掩码），缺测分量从
  `H`/`R` 中按行删除后在观测子空间内做标准更新（等价于降维模型，有测试对拍）；
  整步全缺测时退化为纯预测。连续任意步缺测均可。
- **数值稳定的协方差更新**：
  - 创新协方差 `S = H P H' + R` 经对称化后做 Cholesky 分解，只用三角求解，
    **不显式求逆**；
  - 后验协方差采用 **Joseph 形式** `(I-KH)P(I-KH)' + K R K'` 并对称化；
  - 每步检查对称性与半正定，超过容差抛出带状态码的异常，不返回带 NaN 的结果。
- **严格的输入校验**：维度自洽、元素有限且有界、`Q/R/P0` 对称且半正定。
- 确定性的失败状态码（见下），JSON 接口下单步数值错误不中断整段运行。

## 目录结构

```
kfmu/
  errors.py      异常类型与稳定状态码
  config.py      容差、输入范围、规模上限
  validation.py  维度/有限性/对称/半正定校验
  linalg.py      Cholesky + 三角求解（不求逆）、对称化
  model.py       LinearKalmanModel（F, H, Q, R[, B]）
  filter.py      KalmanFilter / predict / update（缺测核心逻辑）
  runner.py      序列批处理（逐步容错，错误步保留先验并继续）
  jsonio.py      JSON 请求解析与响应序列化（严格 JSON，null=缺测）
  cli.py         python -m kfmu.cli 命令行
tests/           32 个自动化测试（unittest）
examples/        请求/响应样例、库用法、验收轨迹生成脚本
run_tests.sh     一键测试
```

## 安装与环境

Python ≥ 3.10，唯一依赖 NumPy（开发环境实测 Python 3.12.3 + NumPy 2.5.3）。

```bash
pip install -r requirements.txt        # 或 pip install -e .
```

在仓库根目录即可直接 `python3 -m unittest ...` / `python3 -m kfmu.cli ...`，
无需安装。

## 数值容差与输入范围

定义在 `kfmu/config.py`：

| 参数 | 默认值 | 含义 |
|---|---|---|
| `SYMMETRY_RTOL` / `SYMMETRY_ATOL` | `1e-9` / `1e-12` | 对称检查：`max|A−A'| ≤ atol + rtol·max(1, max|A|)` |
| `COV_SYM_FLOOR` | `1e-12` | 协方差更新后对称容差的绝对下限 |
| `PSD_RTOL` / `PSD_FLOOR` | `1e-9` / `1e-10` | 半正定判定：`eig_min ≥ −(PSD_FLOOR + PSD_RTOL·max(1, eig_max))` |
| `MAX_ABS_VALUE` | `1e12` | 所有输入元素绝对值上限（拒绝 NaN/inf/超大值） |
| `MAX_STATE_DIM` / `MAX_MEAS_DIM` | 128 / 128 | 状态维 `n`、量测维 `m` 上限 |
| `MAX_STEPS` | 100 000 | 单次批处理步数上限 |

说明：输入噪声协方差 `Q`/`R`/`P0` 只要求**对称半正定**（允许零噪声、奇异
`R`，只要创新协方差 `S` 仍满秩即可更新）。`S` 奇异时 Cholesky 失败，抛
`numerical_stability_failure / singular_innovation_covariance`。

### 失败状态码

| `code` | 触发条件 | 层级 |
|---|---|---|
| `dimension_mismatch` | 矩阵/向量形状不符 | 致命（请求被拒） |
| `invalid_value` | NaN/inf/超范围、掩码非布尔 | 致命 |
| `invalid_request` / `invalid_json` | JSON 结构/语法错误 | CLI 退出码 2 |
| `matrix_property_violation` | 非方阵、不对称、Q/R/P0 非 PSD | 致命 |
| `singular_innovation_covariance` | `S` 在数值上奇异（Cholesky 失败） | 单步错误 |
| `covariance_not_psd` / `covariance_asymmetry` | 更新后协方差越过 PSD/对称容差 | 单步错误 |

批处理/JSON 中，单步错误记录为 `steps[i].status="error"`、滤波器保留该步
**先验**并继续后续步；CLI 默认退出码仍为 0，加 `--strict-step-errors` 时
返回 3。致命错误输出错误信封 `{"ok": false, "error": {...}}`，退出码 2。

## 库接口

```python
import numpy as np
from kfmu import KalmanFilter, LinearKalmanModel

F = np.array([[1, 1], [0, 1.0]])                 # 状态 n=2：位置、速度
H = np.array([[1.0, 0.0]])                       # 只观测位置，m=1
Q = 0.002 * np.array([[0.25, 0.5], [0.5, 1.0]]) # 过程噪声 PSD
R = np.array([[1.0]])                            # 量测噪声 PSD

model = LinearKalmanModel(F=F, H=H, Q=Q, R=R)
kf = KalmanFilter(model, x0=np.zeros(2), P0=np.eye(2) * 10)

kf.predict()
result = kf.update(np.array([1.3]))              # 正常量测
kf.predict()
result = kf.update(np.array([np.nan]))           # 全缺测 → 纯预测
result = kf.update(z, available=np.array([False, True]))  # 分量级缺测
```

`StepResult.status` 为 `"updated"` 或 `"predicted_only"`，并携带观测子空间内的
`innovation`、`S`、增益 `K` 与可用分量掩码。另有无状态函数
`kfmu.predict(model, x, P)` / `kfmu.update(...)` 和序列批处理
`kfmu.run_batch(...)`。

运行 `python3 examples/library_usage.py` 可看完整输出。

## JSON 接口

请求（`examples/request_example.json`）：

```json
{
  "model": {"F": [[1,1,0,0],[0,1,0,0],[0,0,1,1],[0,0,0,1]],
            "H": [[1,0,0,0],[0,0,1000,0]],
            "Q": [[...对称 PSD 4x4...]],
            "R": [[1,0],[0,160000]]},
  "initial_state": {"x": [0,0,0,0], "P": [[...100·I...]]},
  "measurements": [[0.31,-410.2], [1.72,null], null, null, [null,-2090.7]],
  "masks": null,
  "controls": null,
  "options": {"return_covariance": true, "diagnostics": true}
}
```

缺测约定：

- 某步为 `null` → 该步完全无量测（纯预测）；
- 向量分量为 `null` → 该分量缺测（其余分量正常更新）；
- 可选 `masks`：与量测同形的布尔数组，与 `null` 取**交集**；
- 不接受 `NaN`/`Infinity` JSON 字面量（解析即拒绝）。

响应含 `ok`、`summary`（更新/缺测/错误步数、全程最大协方差不对称量与最小
特征值）、`final` 以及逐步 `steps`（状态、掩码、创新、`x`/`P`、
`diagnostics.max_asymmetry / min_eigenvalue` 或 `error`）。

命令行：

```bash
python3 -m kfmu.cli -i examples/request_example.json -o /tmp/out.json
cat request.json | python3 -m kfmu.cli --strict-step-errors > response.json
```

## 验收：合成恒速轨迹

`tests/test_acceptance.py` 与 `examples/generate_acceptance_demo.py` 构造
4 维二维恒速轨迹（状态 `[x, vx, y, vy]`，160 步，固定种子 20260923）：

1. **不同量纲**：x 轴量测单位米（噪声 σ=1 m），y 轴量测单位**毫米**
   （`H` 第二行带 1000 倍缩放，σ=400 mm，`R` 对角元相差 2.56e8 倍）；
2. **连续缺测**：第 60–74 步连续 15 步全缺测，第 75–89 步仅缺 y 轴；
3. **奇异创新协方差**：用 `Q=R=P0=0` 的退化模型验证 `S=0` 时每步报
   `singular_innovation_covariance`、状态保持有限、批处理不中断；
4. 每一步检查协方差对称性（≤ 1e-9）与 PSD 容差（最小特征值 ≥ −1e-8）；
5. 完整场景经 `process_request` JSON 端到端跑通。

实测末态（生成脚本输出）：`[317.886, 2.010, -238.551, -1.575]`，
真值 `[318, 2, -238.5, -1.5]`，位置误差 < 0.2 m，且缺测段不确定度单调增长、
恢复量测后重新收敛。

## 测试与实测结果

```bash
./run_tests.sh
# 等价于：python3 -m unittest discover -s tests -v
```

最近一次完整运行（Python 3.12.3 / NumPy 2.5.3）：**32 个测试全部通过**
（`Ran 32 tests ... OK`），覆盖：

- `test_filter.py`（18 个）：稳态跟踪 RMSE、全/部分缺测与降维模型对拍、
  连续缺测后重收敛、奇异 `S`、奇异 `R` 但 `S` 满秩、25 组随机模型的对称/PSD、
  米/毫米量纲、各类维度与非法输入拒绝、控制输入；
- `test_jsonio.py`（9 个）：null 语义、掩码交集、单步错误不致命、错误信封、
  严格 JSON 无 NaN、CLI 往返与退出码 0/2/3；
- `test_acceptance.py`（5 个）：上述验收场景。

调参过程中如实记录的两处情况：

- 最初验收场景过程噪声取 `q=0.5`，相对量测噪声过大，把恒速运动当成强机动，
  速度误差达 0.85；扫描后定为 `q=0.005`（误差降到 0.13/0.08）。
- 一维稳态位置 RMSE 实测在 0.4–0.5（离散恒速模型 + σ=1 量测的合理水平，
  20 个种子最大 0.53），测试阈值据此设为 0.65，而非凭感觉写 0.3。

无未通过项。
