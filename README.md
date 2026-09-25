# 校准误差评估（Calibration Error Evaluation）

纯后端的二分类**概率校准评估**本地服务。仅用 Python + NumPy 实现，
**不下载任何外部模型或数据**，用固定随机种子的合成数据和一个简单
线性模型验证核心机制。无前端、无 Web 框架依赖（仅标准库 + NumPy）。

## 功能

- 三个校准指标，定义见下：
  - **Brier 分数**（均方概率误差）
  - **对数损失**（二分类交叉熵，支持三种概率端点策略）
  - **ECE**（等宽分箱期望校准误差，返回逐箱明细）
- **样本权重**贯穿全部指标；保证"加权计算 = 把样本按权重复制后计算"。
- 严格的边界输入校验：非法概率（`<0`、`>1`、`NaN`、`Inf`）、
  非法标签、长度不一致、空输入、非法权重等，返回结构化错误。
- JSON 风格服务层（统一请求/响应信封）+ 命令行入口。
- 可复现合成数据演示（固定种子），含极端类别不均衡场景。

## 指标定义

设样本数 n，标签 `y_i ∈ {0,1}`，正类预测概率 `p_i ∈ [0,1]`，
样本权重 `w_i ≥ 0`，总权重 `W = Σ w_i`。

### Brier 分数（越小越好）

```
BS = (1/W) Σ_i w_i (p_i − y_i)²
```

### 对数损失（越小越好）

```
LL = −(1/W) Σ_i w_i [ y_i ln p_i + (1 − y_i) ln(1 − p_i) ]
```

概率取端点 0/1 时对数可能无定义。`endpoint_strategy` 三选一：

| 策略 | 行为 |
|---|---|
| `clip`（默认） | 逐项裁剪：正类项保证 `p ≥ ε`，负类项保证 `1−p ≥ ε`。于是端点上**预测正确**（`y=1,p=1` 或 `y=0,p=0`）损失恰为 0，**预测错误**损失恰为 `−ln ε`。`ε` 默认 `1e-15`。 |
| `error` | 出现发散项（`y=1,p=0` 或 `y=0,p=1`）且权重为正时直接返回错误。 |
| `ignore` | 跳过发散样本（约定 `0·ln 0 = 0`），归一化分母只含未跳过样本；若全部发散则报错。 |

### ECE（等宽分箱，越小越好）

把 `[0,1]` 等分为 `n_bins` 个半开区间（最后一个含右端点 1）：
`[0,1/B), …, [(B−1)/B, 1]`，概率 `1.0` 归入最后一箱。对每个非空箱 b：

```
W_b    = 箱内权重和
acc_b  = Σ(w·y)/W_b        （箱内加权正类比例）
conf_b = Σ(w·p)/W_b        （箱内加权平均概率）
ECE    = Σ_{非空 b} (W_b / W_非空) · |acc_b − conf_b|
```

空箱权重为 0，不参与求和；样本全覆盖时 `W_非空 = W`。

## 安装

```bash
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt
```

环境要求 Python ≥ 3.10，运行期仅依赖 NumPy。

## 命令行用法

```bash
# 1) 从 JSON 请求文件评估
python -m calibration.cli evaluate examples/request_basic.json

# 2) 从标准输入评估
cat examples/request_weighted.json | python -m calibration.cli evaluate -

# 3) 可复现合成数据演示（真实概率 vs 故意欠校准模型）
python -m calibration.cli demo --n-samples 1000 --seed 42

# 4) 极端类别不均衡（基准正类比例 0.5%）
python -m calibration.cli demo --n-samples 2000 --prior 0.005 --temperature 0.5
```

成功退出码 0，业务错误退出码 1（响应信封中带错误码）。

## 请求 / 响应格式

请求（`sample_weight`、`n_bins`、`endpoint_strategy`、`epsilon` 均可省略）：

```json
{
  "y_true": [0, 1, 1, 0],
  "proba":  [0.1, 0.9, 0.8, 0.4],
  "sample_weight": [1, 1, 2, 1],
  "n_bins": 5,
  "endpoint_strategy": "clip",
  "epsilon": 1e-15
}
```

成功响应（节选）：

```json
{
  "success": true,
  "data": {
    "n_samples": 4, "total_weight": 5.0, "n_bins": 5,
    "endpoint_strategy": "clip",
    "brier_score": 0.04, "log_loss": 0.18, "ece": 0.12,
    "bins": [ {"bin": 0, "range": [0.0, 0.2], "count": 2,
               "weight": 2.0, "mean_proba": 0.25,
               "positive_rate": 0.0, "gap": 0.25}, ... ]
  },
  "error": null
}
```

失败响应：

```json
{"success": false, "data": null,
 "error": {"code": "INVALID_PROBABILITY", "message": "proba 必须落在 [0, 1]，第 2 个样本的概率为 1.2"}}
```

错误码：`INVALID_REQUEST`、`MISSING_FIELD`、`INVALID_INPUT`、
`EMPTY_INPUT`、`INVALID_LABEL`、`INVALID_PROBABILITY`、`LENGTH_MISMATCH`、
`INVALID_WEIGHT`、`INVALID_N_BINS`、`INVALID_EPSILON`、`INVALID_STRATEGY`、
`ENDPOINT_LOSS`、`INVALID_JSON`。

## Python API

```python
from calibration import (
    brier_score, log_loss, expected_calibration_error,
    evaluate_calibration, CalibrationService, make_demo_dataset,
)

brier_score([0, 1], [0.1, 0.9])                       # 0.02
log_loss([1, 0], [0.0, 0.2], endpoint_strategy="ignore")
ece, bins = expected_calibration_error(y, p, n_bins=10, return_bins=True)
result = evaluate_calibration(y, p, sample_weight=w, n_bins=10)

resp = CalibrationService().evaluate({"y_true": y, "proba": p})
```

## 合成数据模型

全部随机数来自固定种子的 `numpy.random.default_rng(seed)`，结果可复现：

1. 单特征 `X ~ N(0,1)`；
2. 真实对数几率 `z = ln(π/(1−π)) + 1.5·X`（`π` 为 `prior`）；
3. 真实概率 `p_true = sigmoid(z)`；
4. 标签 `y ~ Bernoulli(p_true)`；
5. 演示模型 `p_model = sigmoid(1.5·X / T)`：`T>1` 欠自信、`T<1` 过自信，
   从而人为制造校准误差。

## 运行测试

```bash
python -m unittest discover -s tests -v          # 零额外依赖
python -m coverage run --source=calibration -m unittest discover -s tests
python -m coverage report -m
```

## 项目结构

```
.
├── calibration/            # 核心包
│   ├── errors.py           # 统一异常与错误码
│   ├── validation.py       # 边界输入校验
│   ├── metrics.py          # Brier / 对数损失 / ECE
│   ├── synthetic.py        # 可复现合成数据与简单模型
│   ├── service.py          # JSON 风格服务层（统一信封）
│   └── cli.py              # 命令行入口
├── tests/                  # 72 个 unittest 用例
├── examples/               # 请求样例 JSON
├── requirements.txt
├── README.md
└── RUN_REPORT.md           # 实际运行命令与结果记录
```
