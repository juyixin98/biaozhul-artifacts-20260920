# 实际运行记录（RUN_REPORT）

本文件记录开发与验收过程中**实际执行**的命令、结果与中途失败项，
未做修饰。日期：2026-09-25。

## 1. 环境

| 项 | 值 |
|---|---|
| 操作系统 | Linux 6.8.0-90-generic |
| Python | 3.12.3 |
| NumPy | 2.5.3 |
| 测试框架 | 标准库 `unittest`（运行期不依赖 pytest） |
| 覆盖率工具 | coverage 7.16.1（仅用于度量，非运行依赖） |

运行期唯一依赖：NumPy。未下载任何外部模型或数据集，合成数据全部
由固定种子的 `numpy.random.default_rng` 在本地生成。

## 2. 中途失败记录（TDD 真实过程）

### 第一轮：61 个用例，3 个失败

命令：`python3 -m unittest discover -s tests -v`

结果：`Ran 61 tests ... FAILED (failures=3)`

1. `EndpointStrategyTest.test_clip_equals_manual_clipping`
   期望 `-ln(1e-10)=23.0258509299`，实得 `23.0258508885`。
   **原因（实现缺陷，已修）**：初版把概率对称裁剪到 `[ε, 1−ε]`，
   浮点上 `1−(1−ε)` 存在抵消误差；且对称裁剪会让"端点预测正确"
   （如 `y=0,p=0`）也产生非零损失。已改为**逐项裁剪**：正类项
   保证 `p ≥ ε`、负类项保证 `1−p ≥ ε`。
2. `IdenticalProbabilitiesTest.test_all_probability_extreme_wrong_label_clip`
   同一根因（`1−(1−1e-12)` 抵消，差 `2.2e-5`），随上一项一并修复。
3. `EndpointStrategyTest.test_ignore_skips_only_divergent_samples`
   实得 `0.2231`，原期望 `0.9163`。
   **原因（测试手算错误，已修）**：样本 `y=0,p=0.2` 的损失是
   `-ln(1-0.2)=-ln(0.8)=0.2231`，测试误写成 `-ln(0.2)`。实现输出正确，
   修正了测试期望值。

修复后：`Ran 61 tests ... OK`。

### 第二轮：补分支覆盖，71 个用例，1 个失败

1. `ValidationBranchTest.test_nan_label_rejected`：预期错误码
   `INVALID_LABEL`，实得 `INVALID_INPUT`。
   **原因（测试预期不当，已修）**：NaN 标签先被"必须为有限值"检查
   拦截，返回 `INVALID_INPUT` 是合理行为（NaN 既不是 0 也不是 1，
   但更准确的归类是"非有限输入"）。修正测试预期。

修复后：`Ran 71 tests ... OK`。

随后追加 1 个 `epsilon=0` 端点语义固化用例，最终为 72 个用例。

## 3. 最终测试结果

命令：

```bash
python3 -m unittest discover -s tests
```

结果：

```
Ran 72 tests in 0.501s

OK
```

覆盖率命令：

```bash
python3 -m coverage run --source=calibration -m unittest discover -s tests
python3 -m coverage report -m
```

结果：

```
Name                        Stmts   Miss  Cover
-----------------------------------------------
calibration/__init__.py         6      0   100%
calibration/cli.py             57      1    98%
calibration/errors.py           8      0   100%
calibration/metrics.py         63      0   100%
calibration/service.py         27      0   100%
calibration/synthetic.py       33      0   100%
calibration/validation.py      47      0   100%
-----------------------------------------------
TOTAL                         241      1     99%
```

唯一未覆盖行是 `cli.py` 的 `if __name__ == "__main__": raise SystemExit`
入口守卫（由真实子进程冒烟测试 `test_real_subprocess_entrypoint_smoke`
执行，但不计入进程内 coverage）。业务代码覆盖率 100%，满足 ≥80% 要求。

## 4. CLI 实际运行记录

### 4.1 基础请求

```bash
python3 -m calibration.cli evaluate examples/request_basic.json
```

退出码 0。关键输出：

```
brier_score = 0.048437499999999994
log_loss    = 0.21265040782220226
ece         = 0.18124999999999997   (n_bins=5)
```

手算核对 Brier：误差平方和 `0.01+0.01+0.04+0.16+0.1225+0.04+0.0025+0.0025
= 0.3875`，除以 8 = **0.0484375** ✓。

### 4.2 加权请求

```bash
python3 -m calibration.cli evaluate examples/request_weighted.json
```

退出码 0：`total_weight=8.0, brier=0.07750000000000001,
log_loss=0.3015785695695317, ece=0.15 (n_bins=3)`。

手算核对 Brier：加权误差平方和
`0.01+0.01+2·0.04+0.16+2·0.16+0.04 = 0.62`，除以总权重 8 =
**0.0775** ✓。

### 4.3 端点 ignore 请求

```bash
python3 -m calibration.cli evaluate examples/request_endpoints_ignore.json
```

退出码 0。发散样本（`y=1,p=0`）被跳过，其余两样本损失均为 `-ln(0.8)`，
`log_loss = 0.22314355131420974` ✓。

### 4.4 非法概率请求

```bash
python3 -m calibration.cli evaluate examples/request_invalid_probability.json
echo $?
```

退出码 1，输出：

```json
{"success": false, "data": null,
 "error": {"code": "INVALID_PROBABILITY",
           "message": "proba 必须落在 [0, 1]，第 2 个样本的概率为 1.2"}}
```

### 4.5 stdin

```bash
echo '{"y_true":[0,1],"proba":[0.3,0.7]}' | python3 -m calibration.cli evaluate -
```

退出码 0，`success=true`。

### 4.6 可复现合成数据 demo

```bash
python3 -m calibration.cli demo --n-samples 1000 --seed 42
```

退出码 0：

```
brier_score = 0.199529
log_loss    = 0.584705
ece         = 0.156718          （temperature=2.0 的欠自信模型）
observed_positive_rate = 0.345
ece_of_true_probability = 0.054766   （生成数据所用真实概率）
```

真实概率的 ECE（0.055）明显低于故意欠校准模型（0.157），机制符合预期。
两次相同参数运行输出**逐字节一致**（`test_demo_is_reproducible` 固化）。

### 4.7 极端类别不均衡

```bash
python3 -m calibration.cli demo --n-samples 2000 --prior 0.005 --temperature 0.5
```

退出码 0，观测正类比例 0.01（2000 个样本中 20 个正类），流程无异常；
该参数下过自信模型 `ece=0.4753`，而真实概率 `ece=0.002863`。

## 5. 独立交叉核对（不使用本库代码）

用一段独立的纯 Python（`math` + 显式循环）参考实现重算，与本库对比：

```
dataset A (y=[0,1,1,0], p=[0,.5,1,.5]):
    参考 (0.125, 0.3465735903, 0.0)
    本库 (0.125, 0.3465735903, 0.0)        # LL=ln2/2
weighted example:
    参考 (0.0775, 0.3015785696, 0.15)
    本库 (0.07750000000000001, 0.3015785696, 0.14999999999999997)
basic example:
    参考 (0.0484375, 0.2126504078, 0.18125)
    本库 (0.048437499999999994, 0.2126504078, 0.18124999999999997)
```

差异仅在浮点末位，定义层面完全一致。

## 6. 验收项对照

| 验收要求 | 落实位置 | 状态 |
|---|---|---|
| 手算数据核对 | `tests/test_hand_verified.py`（数据集 A/B/C，期望值纸笔推导）；本文件 §4、§5 | 通过 |
| 加权复制等价 | `tests/test_weighted.py`：整数复制、分数权重放大、权重尺度不变、零权重无效 | 通过 |
| 全同概率 | `test_edge_cases.py::IdenticalProbabilitiesTest`（全 0、全 .5、全 1、p≡.2） | 通过 |
| 极端类别不均 | `ExtremeImbalanceTest`（10000:1）+ §4.7 demo（prior=0.005） | 通过 |
| 非法概率 | `InvalidInputTest`：<0、>1、NaN、Inf，及非法标签/权重/长度/空输入/参数 | 通过 |
| 概率端点策略 | `EndpointStrategyTest`：clip/error/ignore 三策略及加权 ignore | 通过 |
| Brier / 对数损失 / ECE 定义明确 | `calibration/metrics.py` 模块文档 + README「指标定义」 | 完成 |
| 支持样本权重 | 三个指标全部加权，复制等价测试保证语义 | 通过 |
| 源码、README、请求样例、自动化测试 | `calibration/`、`README.md`、`examples/*.json`、`tests/`（72 例） | 交付 |
| 实际运行并如实记录 | 本文件 | 完成 |
| 不做前端 | 仅 Python 包 + CLI，无任何前端代码 | 满足 |

## 7. 未通过项与已知限制

- **未通过项：无。** 最终 72 个用例全部通过；开发中途的 4 个失败
  （3 个实现/手算问题、1 个测试预期问题）已在 §2 记录并修复。
- 覆盖率为 99%，唯一缺口是 `__main__` 入口守卫（非业务逻辑）。
- ECE 采用**等宽分箱**（equal-width），未实现等频（equal-mass）分箱；
  分箱归一化分母只含非空箱权重。此为明确的定义选择，非缺陷。
- 服务层为**本地** JSON 字典接口（进程内调用 / stdin 文件），未绑定
  HTTP 框架；如需网络暴露，可在 `CalibrationService.evaluate` 之外
  包一层框架适配，业务逻辑无需改动。
- `clip` 策略的 `epsilon` 合法区间为 `[0, 0.5)`；`epsilon=0` 表示不做
  抬升，此时若存在发散损失会得到 `inf`（与 NumPy 语义一致）。
