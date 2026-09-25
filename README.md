# 校准误差评估(Calibration Error Evaluation)

纯后端 Python + NumPy 项目:二分类概率校准评估的本地机器学习基础设施服务。
不下载任何外部模型或数据 —— 用固定种子的合成数据和纯 NumPy 实现的简单模型(逻辑回归)验证核心机制。

## 指标定义

设样本 $i$ 的标签 $y_i \in \{0,1\}$、预测概率 $p_i \in [0,1]$、样本权重 $w_i \ge 0$,权重总和 $W = \sum_i w_i$。所有指标均为**加权**形式,权重缺省为全 1。

### Brier 分数

$$BS = \frac{1}{W} \sum_i w_i (p_i - y_i)^2$$

越小越好,完美预测为 0,恒定预测 0.5 时为 0.25。

### 对数损失(Log Loss)

$$LL = -\frac{1}{W} \sum_i w_i \big[ y_i \ln p_i + (1 - y_i) \ln(1 - p_i) \big]$$

### ECE(期望校准误差)

把 $[0,1]$ 等宽划分为 $B$ 个箱子(`n_bins`,默认 10)。第 $b$ 箱内:

- $\mathrm{acc}(b)$ = 箱内加权正例率
- $\mathrm{conf}(b)$ = 箱内加权平均预测概率

$$ECE = \sum_{b=1}^{B} \frac{W_b}{W} \big| \mathrm{acc}(b) - \mathrm{conf}(b) \big|$$

空箱跳过。分箱规则:`floor(p * n_bins)`,即 $[0, \frac{1}{B}), [\frac{1}{B}, \frac{2}{B}), \dots$,**最后一箱含右端点 1.0**;`p=0.0` 进第一箱。

## 概率端点策略(`endpoint_strategy`)

端点 `p=0` / `p=1` 是合法概率,但会使对数损失发散,因此提供两种策略(**仅影响对数损失**):

| 策略 | 行为 |
|---|---|
| `clip`(默认) | 概率裁剪到 $[\varepsilon, 1-\varepsilon]$,$\varepsilon$ 默认 `1e-15`,损失始终有限 |
| `allow` | 不裁剪;预测与标签在端点冲突时(如 `p=0, y=1`)对数损失为 `+inf`(API 中序列化为 `"inf"`),不冲突时按约定 $0 \ln 0 = 0$ |

**区间外的概率**(`p < 0`、`p > 1`、NaN、±inf)无论哪种策略都在校验阶段抛出 `ValueError`(HTTP 层返回 400)。

## 项目结构

```
calibration_eval/
├── __init__.py        # 包入口,导出公开 API
├── validation.py      # 输入校验(标签/概率/权重)
├── metrics.py         # Brier、LogLoss、ECE、分箱统计、evaluate 汇总
├── service.py         # 本地 HTTP 服务(仅标准库 http.server)
└── synthetic.py       # 可复现合成数据 + NumPy 逻辑回归 + 温度缩放
examples/
├── run_demo.py        # 端到端演示:合成数据 → 训练 → 评估
├── request_example.json  # /evaluate 请求体样例
└── request_example.sh    # curl 请求样例脚本
tests/
├── test_metrics.py    # 指标测试(手算核对、加权复制等价、边界、非法输入)
└── test_service.py    # 服务测试(真实起服务发请求)
```

## 快速开始

```bash
pip install -r requirements.txt   # 仅 numpy + pytest

# 运行测试
python3 -m pytest -v

# 端到端演示(合成数据 + 简单模型)
python3 examples/run_demo.py

# 启动本地服务
python3 -m calibration_eval.service   # http://127.0.0.1:8000
```

## API

### `GET /health`

返回 `{"status": "ok"}`。

### `POST /evaluate`

请求体(见 `examples/request_example.json`):

```json
{
  "y_true": [1, 0, 1, 0],
  "y_prob": [0.9, 0.2, 0.6, 0.4],
  "sample_weight": [1.0, 1.0, 2.0, 1.0],
  "n_bins": 2,
  "endpoint_strategy": "clip"
}
```

- `y_true`、`y_prob`:必填,等长一维数组;
- `sample_weight`:可选,缺省全 1;必须非负且总和 > 0;
- `n_bins`:可选,默认 10;
- `endpoint_strategy`:可选,`"clip"`(默认)或 `"allow"`;
- `epsilon`:可选,默认 `1e-15`。

响应(200):全部指标 + 逐箱统计(`bins` 含每箱样本数、权重、置信度、正例率、校准差,空箱字段为 `null`)。输入非法时返回 400 及中文错误信息。

```bash
curl -s -X POST http://127.0.0.1:8000/evaluate \
  -H 'Content-Type: application/json' \
  -d @examples/request_example.json
```

## 库调用

```python
from calibration_eval import brier_score, log_loss, expected_calibration_error, evaluate

brier_score([1, 0, 1, 0], [0.9, 0.2, 0.6, 0.4])                    # 0.0925
log_loss([1, 0, 1, 0], [0.9, 0.2, 0.6, 0.4])                       # ≈ 0.337539
expected_calibration_error([1, 0, 1, 0], [0.9, 0.2, 0.6, 0.4], n_bins=2)  # 0.275
evaluate([1, 0, 1, 0], [0.9, 0.2, 0.6, 0.4], sample_weight=[1, 1, 2, 1])
```

## 手算核对(验收基准)

`y = [1, 0, 1, 0]`,`p = [0.9, 0.2, 0.6, 0.4]`:

- **Brier** = (0.01 + 0.04 + 0.16 + 0.16) / 4 = **0.0925**
- **LogLoss** = −(ln 0.9 + ln 0.8 + ln 0.6 + ln 0.6) / 4 ≈ **0.337539**
- **ECE(n_bins=2)**:箱 0 {p=0.2,y=0; p=0.4,y=0} conf=0.3、acc=0、gap=0.3;箱 1 {p=0.9,y=1; p=0.6,y=1} conf=0.75、acc=1、gap=0.25;ECE = 0.5×0.3 + 0.5×0.25 = **0.275**

以上数值由 `tests/test_metrics.py` 中的 `test_*_hand_computed` 用例断言。

## 实际运行记录

以下为本项目真实执行结果(2026-09-25,Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1,Linux):

### 1. 自动化测试

```
$ python3 -m pytest -v
============================== 34 passed in 3.77s ==============================
```

34 项全部通过,**无未通过项**。覆盖:手算核对(4 项)、加权复制等价(整数/分数权重/零权重,3 项)、
全同概率、极端类别不均(全正例 / 千分之一稀有正例)、完美预测、端点策略 clip vs allow、
分箱端点归属、非法概率(-0.1 / 1.2 / NaN / inf)、非法标签、长度不一致、空输入、
负权重、零总权重、非法端点策略、非法 n_bins、evaluate 汇总、inf 序列化、合成数据冒烟、
HTTP 服务(健康检查 / 正常与加权请求 / 400 / 404)。

> 备注:开发过程中曾有 1 次失败 —— `test_weighted_replication_equivalence` 中测试代码自身把
> `n_bins` 整数误传为 `sample_weight` 位置参数,修正为关键字传参后通过。被测源码未改动。

### 2. 测试覆盖率

```
$ python3 -m pytest --cov=calibration_eval --cov-report=term-missing -q
TOTAL    189 stmts   32 miss   83%
```

(metrics.py 98%,validation.py 85%,service.py 74%,synthetic.py 67%;总体 83% ≥ 80%)

### 3. 端到端演示

```
$ python3 examples/run_demo.py
=== 真实概率(完美校准参照) ===   Brier = 0.160605  LogLoss = 0.485156  ECE = 0.015833
=== 逻辑回归模型 ===             Brier = 0.159998  LogLoss = 0.483168  ECE = 0.021709
=== 温度缩放 T=3(刻意失准) ===  Brier = 0.186728  LogLoss = 0.559854  ECE = 0.146611
```

完美校准参照的 ECE 接近 0,温度缩放失准模型的 ECE 明显升高,符合预期。

### 4. HTTP 服务实测

```
$ python3 -m calibration_eval.service &
$ bash examples/request_example.sh
== 健康检查 ==        {"status": "ok"}
== 正常评估请求 ==     {"n_samples": 4, "total_weight": 5.0, "brier_score": 0.106..., "ece": 0.3, ...}
== 非法概率 ==        {"error": "y_prob 必须落在 [0, 1] 区间内, 发现非法值: [-0.1]"}  (HTTP 400)
```

加权样例(`w=[1,1,2,1]`)手算复核:Brier = (0.01+0.04+2×0.16+0.16)/5 = 0.106 ✓;
ECE = (2/5)×0.3 + (3/5)×0.3 = 0.3 ✓,与服务返回一致。
