# Tiny Integer MatMul Inference Backend

一个小型的**非对称激活 + 逐输出通道权重量化**整数乘加（matmul + bias）
推理后端。Python · FastAPI · NumPy，无外部服务、无真实业务数据，所有
夹具均为固定种子的本地合成数据。

## 它能回答什么问题

测试不是“接口能调用”式的冒烟测试，而是断言**具体整数结果与失败类别**：

1. **手算小矩阵逐元素验证**（`tests/test_kernel_handcalc.py`）
   原始点积、零点修正项、bias 码、缩放/舍入/饱和后的 int8 输出，期望值
   全部由人手算出并写死（见该文件顶部的推导），不调用被测核或它的
   oracle 来生成答案。
2. **极值、非零零点、通道不同尺度**
   `-128/127` 码、`za=127`、`zw=-128/127`、每通道不同 scale/zp 都有
   独立用例。
3. **独立参考答案**（`app/verification.py`）
   - 精确整数参考：**纯 Python 三重循环 + 任意精度 int**，与
     `app/kernel.py` 没有任何共享代码路径；
   - 浮点参考：对**原始未量化权重**的 float64 `x @ W.T + b`，不是对
     核输出反量化得到的。
4. **与浮点模型比较误差，但不要求相等**
   每层给出数据相关的解析误差上界（预算），判定
   `max_abs_error <= budget`，并显式断言量化输出与浮点结果**不**逐位相等。
5. **失败类别可断言**：整数结果被篡改 → `REJECTED`；越界输入导致输出
   饱和、误差预算被破坏 → `UNDETERMINED`；版本不匹配 / 形状错误 / NaN
   → 对应的 `REJECTED_*`。

## 数值约定（顺序是契约的一部分）

对每张量激活参数 `(sa, za)`、逐输出通道权重参数 `(sw[j], zw[j])`：

1. `raw[i,j] = Σ_k qa[i,k]·qw[j,k]`
2. 零点修正（每通道）：
   `acc = raw − zw[j]·Σqa − za·Σqw + K·za·zw[j] + bias_q[j]`
3. **bias 的尺度是累加器尺度 `sa·sw[j]`**（由
   `kernel.quantize_bias` 量化），绝不是输出尺度。
4. 再量化严格按此顺序：
   `yq = saturate_int8( round_half_away(acc · (sa·sw[j]/sy[j])) + zy[j] )`
   - 乘法因子用 float64；
   - 舍入为确定性的 **round-half-away-from-zero**（不是 NumPy 的银行家
     舍入，也不依赖宿主 C 库当前舍入模式）；
   - 先加输出零点，再在窄化转换**之前**饱和到 `[-128,127]`。

### 累加位宽与溢出

语义累加器是 **int32**。所有中间整数运算在一个**受检查的 int64 工作区**
中完成，使宿主语言的整数回绕永远无法决定结果；每个阶段都检查 int32
包络，越界即抛 `AccumulatorOverflow`（服务映射为
`UNDETERMINED`），绝不静默回绕或静默饱和。`K·128² < 2³¹`（`K≤131071`）
时 int32 天然安全，超过也不会假设有保证。

### 校准与模型版本绑定

校准只在**离线**发生一次（`app/calibration.py` 的 observer →
`CalibrationBuilder.build()` → 冻结 `CalibrationBundle`）。bundle 携带
`model_id` + `model_version`，并对全部量化参数计算 SHA-256 指纹；
`ModelLifecycle` 的 `CALIBRATING → FROZEN` 转换单向且只能发生一次。
注册表把唯一指纹绑定到部署版本，请求路径上没有任何重新估计的入口
（测试 `test_repeated_requests_never_change_calibration`、
`test_frozen_calibration_cannot_be_re_estimated` 对此断言）。

## 模块职责

| 模块 | 真实职责 |
|---|---|
| `app/tensor_types.py` | dtype/界值、affine 映射、逐张量/逐通道 `QuantSpec`、显式饱和 |
| `app/kernel.py` | 定点 matmul：零点修正、位宽检查、缩放/舍入顺序、bias 尺度 |
| `app/calibration.py` | 离线 min/max observer、冻结的不可变校准 bundle 与指纹 |
| `app/training_state.py` | 校准生命周期状态机（冻结后不可变） |
| `app/graph.py` | 量化层、计算图、版本绑定的模型注册表 |
| `app/verification.py` | 独立纯 Python 整数 oracle、float64 参考、误差预算与裁决 |
| `app/errors.py` | 结构化错误分类（ACCEPTED / REJECTED / UNDETERMINED） |
| `app/schemas.py` | 边界请求/响应校验 |
| `app/main.py` | FastAPI 入口、请求标识、脱敏日志、错误语义 |
| `app/bootstrap.py` | 从本地合成夹具离线校准并装配只读注册表 |
| `scripts/generate_fixtures.py` | 固定种子生成合成权重/偏置/校准批 |
| `scripts/demo.py` | 不起服务的本地端到端演示 |
| `tests/` | 手算整数、独立 oracle/预算、API 失败类别三组测试 |

## 快速开始

```bash
pip install -r requirements.txt

# 1) 生成固定种子的本地合成夹具（仓库内已附带一份，可重复生成）
python3 scripts/generate_fixtures.py

# 2) 本地演示（打印整数中间量、oracle 对照、误差与裁决）
python3 -m scripts.demo

# 3) 起服务
python3 -m uvicorn app.main:app --host 127.0.0.1 --port 8000
# 或：./run.sh
```

探测：

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/models/tiny-matmul-demo
curl -s -X POST http://127.0.0.1:8000/infer \
  -H 'content-type: application/json' \
  -d '{"model_id":"tiny-matmul-demo","model_version":"2026-09-28-v1",
       "shape":[2,4],
       "values":[0.5,-1,0.25,0.75,-0.5,1,-0.25,-0.75],
       "request_id":"req-demo-1"}'
```

## 运行测试

```bash
python3 -m pytest                       # 全部 31 项
python3 -m pytest --cov=app --cov-report=term-missing   # 覆盖率（约 90%）
```

测试会在临时目录里自行生成夹具，不依赖仓库内的 `fixtures/`。

## 错误语义与复现

每个响应（含错误）都带同一个 `request_id`（未提供则生成 `req-xxxx`）。
日志只记录 `request_id`、形状、计数与聚合误差，**绝不打印调用方张量
数值**。

| HTTP | code | verdict | 含义 / 为什么这样裁决 |
|---|---|---|---|
| 200 | — | `ACCEPTED` | 整数结果与独立 oracle 完全一致，且每层误差在数据相关预算内 |
| 200 | — | `UNDETERMINED` | 整数路径正确，但误差预算被破坏（典型：输出饱和），结果不可判定 |
| 422 | `REJECTED_INVALID_INPUT` | `REJECTED` | 形状/值数不符、维度不符、NaN/Inf、未知模型 |
| 409 | `REJECTED_VERSION_MISMATCH` | `REJECTED` | 请求版本不是当前绑定版本；details 给出版本与校准指纹 |
| 409 | `REJECTED_NOT_CALIBRATED` | `REJECTED` | 模型冻结前被调用（防御性） |
| 422 | `UNDETERMINED_NUMERIC_CONTRACT` | `UNDETERMINED` | int32 累加包络被越过，无法判定数值结果 |
| 500 | `REJECTED_CORE_INTEGRITY` | `REJECTED` | 整数核与独立精确 oracle 不一致（实现缺陷，非调用方问题） |
| 500 | `REJECTED_INTERNAL` | `REJECTED` | 非预期服务缺陷，生成新请求 id 便于复现 |

复现一次失败：拿响应里的 `request_id` 到服务端日志检索，可看到形状、
每层 `max_abs_error / max_error_budget / saturated_outputs / violations`
等关键状态，据此说明是**接受、拒绝还是无法判定**，以及原因。例如越界
输入会得到 `UNDETERMINED`，verification.reasons 指明哪一层多少元素超出
预算、饱和数量是多少。

## 配置

`app/config.py` 集中管理，可用环境变量覆盖：
`QINFER_MODEL_ID`、`QINFER_MODEL_VERSION`、`QINFER_FIXTURE_DIR`、
`QINFER_MAX_BATCH`、`QINFER_MAX_FEATURES`。夹具 meta 的版本与服务配置
不一致时启动即失败（防止错配模型与校准）。
