# IMU 静止段识别与陀螺零偏估计服务

纯后端离线服务：输入 IMU 加速度、角速度与采样时间，先基于**滑窗方差 + 重力幅值**
识别静止候选区间，再用**稳健加权回归（Huber IRLS）**估计陀螺零偏（含温漂/时漂模型），
输出候选区间、逐窗残差与置信指标。运动段不会混入零偏估计；仅凭静止数据不可识别的参数
在响应中显式声明，绝不报告为"已准确求解"。

- 语言/框架：Python 3.10+、NumPy、FastAPI（Pydantic v2 校验）
- 无前端页面；无外部计算服务，所有数值与密码学操作均在本地真实执行

---

## 1. 快速开始（本地启动）

```bash
cd P054/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock      # 锁定依赖，可复现安装
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

生成示例输入（已随仓库提供在 `examples/`，也可重新生成）：

```bash
python scripts/make_example_inputs.py
```

调用（另开一个终端）：

```bash
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/estimate \
  -H "Content-Type: application/json" \
  --data @examples/sudden_motion.json | python -m json.tool
```

或用内置签名客户端：

```bash
python scripts/signed_request.py examples/spikes.json --no-auth \
  --base-url http://127.0.0.1:8000
```

---

## 2. 验收命令

```bash
source .venv/bin/activate
python -m pytest tests/ -v          # 26 个自动化测试（算法 + HTTP + HMAC 真实验签）
```

测试覆盖：

| 场景 | 测试 | 期望行为 |
|---|---|---|
| 温漂合成数据 | `test_temperature_drift_coefficients_recovered` | 选择 `temp_drift` 模型，恢复温漂系数（误差 < 2e-5 rad/s/°C） |
| 突然运动 | `test_static_motion_static_recovers_bias` / `test_motion_does_not_pollute_bias` | 运动时刻不在任何静止区间内，零偏误差 < 3e-3 rad/s |
| 异常峰值 | `test_isolated_spikes_detected_and_robust` | 4 个注入毛刺全部检出，估计值不被带偏 |
| 时间重复 | `test_duplicate_timestamps_consistent_merged` / `..._inconsistent_dropped` | 一致重复合并，不一致重复整组剔除 |
| 时间缺口 | `test_time_gap_segments_separately` | 大缺口切分为独立段分别检测 |
| 无静止 | `test_no_static_returns_not_observable` | 返回 `not_observable_from_data`，置信度 low |
| 单位/坐标轴 | `test_units_degs_and_g_converted` / `test_axes_remapping` | g、°/s、ms 与轴重映射结果与 SI 默认一致 |
| 协议/密码学 | `tests/test_api.py`（12 项） | 422 错误信封、坏 JSON、真实 HMAC 签名通过、篡改/错密钥/重放拒绝、响应签名可验 |

手工 HMAC 端到端验收：

```bash
# 终端 A：以密钥启动
IMU_BIAS_HMAC_SECRET="acceptance-secret-2026" \
  uvicorn app.main:app --host 127.0.0.1 --port 8000

# 终端 B：签名请求（脚本同时验证响应头 X-Response-Signature）
IMU_BIAS_HMAC_SECRET="acceptance-secret-2026" \
  python scripts/signed_request.py examples/spikes.json
```

无签名、错密钥、±300s 外时间戳均返回 `401` 与统一错误信封。

---

## 3. 处理流程

```
输入 JSON
  └─ Pydantic 校验（长度一致、三分量、有限值、单位/坐标轴合法）
     └─ 预处理
         ├─ 单位换算为 SI（m/s²、rad/s、s）
         ├─ 坐标轴重映射到内部 XYZ 右手系（可变号/重排）
         ├─ 时间回退 → 422 拒绝（不猜测顺序）
         ├─ 重复时间戳：组内一致 → 均值合并；不一致 → 整组剔除并计数
         └─ 时间缺口（gap > time_gap_factor × 中位间隔）→ 切段
     └─ 异常峰值审计（逐样本稳健 z 分数 + 孤立性判据，区分毛刺与持续运动）
     └─ 静止检测（按时间段独立执行）
         ├─ 重叠滑窗（默认 0.5s、50% 重叠）
         ├─ 逐窗陀螺 std ≤ 阈值
         ├─ 逐窗加表 std ≤ 阈值
         └─ |median(‖a‖) − g| ≤ 容差（重力幅值准则）
         └─ 形态学闭运算填平 ≤2 个非静止窗的小缺口
         └─ 区间最短持续时间过滤（默认 1s）
     └─ 陀螺零偏稳健估计（仅使用最终静止区间内的窗）
         ├─ 每窗陀螺中位数为观测，样本数为权
         ├─ Huber IRLS（k=1.345）：异常窗自动降权而非删除
         ├─ 候选模型 constant / time_drift / temp_drift，逐轴选择
         └─ 三明治稳健协方差 + 窗间稳健离散 → 保守标准误与 95% CI
     └─ 置信度评分（窗数、静止时长、窗间离散、跨段重复性）
     └─ 输出：候选区间、逐窗残差、置信指标、告警、不可观测参数清单
```

**为什么运动不会混入**：估计的观测只来自通过全部三个准则、且位于最短时长过滤后
静止区间内的窗；区间合并只填平被两侧静止窗夹住的极短缺口，不会跨越持续运动段
（测试以 0.4 rad/s 与 5 rad/s 两种运动幅度验证）。

---

## 4. 仅凭静止数据**不可完整识别**的参数（响应 `unobservable_parameters` 固定声明）

1. **加表零偏 accelerometer_bias**：静止观测为 `a_meas = Rᵀg + b_a`，
   `‖a‖≈g` 只给一个标量约束；垂直重力方向的两个零偏分量完全不可观测。
   本服务在 `accelerometer` 中只报告静止时测得的**比力矢量**，并明确标注它不是加表零偏。
2. **加表刻度因子 / 非正交**：比力幅值恒为 g 时与零偏、姿态不可分。
3. **陀螺刻度因子**：静止时 `ω_true=0`，`S·0` 不携带任何 S 的信息，需要已知转速激励。
4. **陀螺安装非正交**：同理需要绕各轴的已知旋转。
5. **陀螺 g-sensitivity（加速度敏感项）**：与零偏、重力方向耦合，需要多方位静止。
6. **时漂与温漂同时辨识**：单调升温数据中 t 与 T 强共线；两者都显著时只选其一并告警。

无静止区间时 `gyroscope_bias.status = "not_observable_from_data"`，
置信度强制 `low`，响应中不含任何零偏数值。

---

## 5. HTTP 协议

### `GET /health` / `GET /ready`

`/ready` 返回 `hmac_auth_enabled` 指示鉴权是否开启。

### `POST /estimate`

请求体：

```json
{
  "timestamps": [0.0, 0.01, 0.02],
  "accel": [[0.0, 0.0, 9.80665], [0.0, 0.0, 9.8067]],
  "gyro":  [[0.01, -0.02, 0.005], [0.01, -0.02, 0.005]],
  "temperature": [20.0, 20.0, 20.01],
  "units": {"accel": "ms2", "gyro": "rads", "time": "s"},
  "axes":  {"x": "+x", "y": "+y", "z": "+z"},
  "detection": {
    "window_seconds": 0.5,
    "window_overlap": 0.5,
    "min_static_seconds": 1.0,
    "gyro_std_thresh": 0.02,
    "accel_std_thresh": 0.05,
    "gravity_mag_tol": 0.25,
    "bridge_max_gap_windows": 2,
    "time_gap_factor": 5.0,
    "spike_z": 8.0,
    "temp_min_windows": 8,
    "temp_min_span": 5.0,
    "drift_min_windows": 8,
    "drift_min_span": 30.0
  }
}
```

- `temperature` 可选；`units`/`axes`/`detection` 均可省略走默认值。
- 单位（**显式配置**）：
  - `accel`: `ms2`（默认）或 `g`
  - `gyro`: `rads`（默认）、`degs`（°/s）、`rad_h`、`deg_h`、`rpm`
  - `time`: `s`（默认）、`ms`、`us`、`ns`
- 坐标轴（**显式配置**）：`axes.x/y/z` 取值 `±x/±y/±z`，表示内部轴取自哪一输入列
  （含符号）；三个内部轴必须各引用一个输入轴，否则 422。
- 所有检测阈值均为 SI 单位（rad/s、m/s²、s、°C）。

响应（关键字段）：

```jsonc
{
  "ok": true,
  "summary": { "gyro_bias_status": "estimated", ... },
  "preprocessing": { "duplicate_samples_merged": 2, "time_gaps": 1, "segments": 2, ... },
  "spikes": { "total_samples_flagged": 8, "signals": [{"signal": "gyro", "indices": [...]}] },
  "static_intervals": [
    { "t_start_s": 0.0, "t_end_s": 19.74, "duration_s": 19.74,
      "observed_specific_force_xyz_ms2": [...],
      "gravity_magnitude_residual_ms2": 0.001,
      "note": "该矢量是静止时测得的比力...不是加表零偏估计值。" }
  ],
  "window_residuals": [
    { "t0_s": 0.0, "t1_s": 0.49,
      "gyro_median_rad_s": [...], "gyro_residual_rad_s": [...],
      "gyro_residual_norm_rad_s": 0.0003,
      "gyro_std_rad_s": [...], "accel_norm_residual_ms2": 0.001,
      "huber_weight_xyz": [1.0, 1.0, 1.0] }
  ],
  "gyroscope_bias": {
    "status": "estimated",
    "bias_at_reference_rad_s": [0.0099, -0.0201, 0.0050],
    "bias_at_reference_deg_s": [0.57, -1.15, 0.29],
    "standard_error_rad_s": [...],
    "ci95_rad_s": [[lo, hi], ...],
    "chosen_model_per_axis": {"x": "constant", "y": "temp_drift", "z": "constant"},
    "axes": {
      "x": {"model": "constant", "bias_at_reference": ..., "bias_se": ...,
            "candidate_models": [...], "time_drift_eligible": true, "temp_drift_eligible": false}
    }
  },
  "accelerometer": {
    "status": "bias_not_identifiable_from_static_only",
    "orientation_spread_deg": 1.2,
    "multi_orientation_detected": false
  },
  "confidence": {"grade": "high", "score": 0.93, "components": {...},
                 "bias_95ci_halfwidth_rad_s": [...]},
  "unobservable_parameters": [ {"parameter": "accelerometer_bias", "reason": "..."} ],
  "warnings": ["..."]
}
```

错误信封（400/401/422/500 统一结构）：

```json
{"ok": false, "error": {"code": "validation_error", "message": "...", "details": {}}}
```

---

## 6. HMAC 鉴权（真实 HMAC-SHA256，非演示桩）

设置环境变量即启用（未设置时鉴权关闭，便于本地调试）：

- `IMU_BIAS_HMAC_SECRET`：直接给密钥；或
- `IMU_BIAS_HMAC_SECRET_FILE`：指向含密钥的文件

客户端对 `f"{X-Timestamp}.{METHOD}.{PATH}.{sha256_hex(body)}"` 计算 HMAC-SHA256，
发送头：

- `X-Timestamp`：Unix 秒（允许小数），与服务器时间偏差 > 300s 拒绝（防重放）
- `X-Signature`：`sha256=<hex>`

服务端用 `hmac.compare_digest` 常量时间比较；响应头 `X-Response-Signature`
为响应体的 HMAC，客户端可用同一密钥验证响应完整性。

---

## 7. 示例文件（`examples/`）

| 文件 | 内容 |
|---|---|
| `temp_drift.json` | 120s 静止 + 20→44°C 线性升温，陀螺零偏随温度线性变化 |
| `sudden_motion.json` | 静止-20s 突然运动-静止，运动幅度大、起止陡峭 |
| `spikes.json` | 静止数据中 4 个 1~2 样本宽的孤立大毛刺 |
| `duplicate_ts.json` | 2 个一致重复时间戳 + 50s 采集缺口（分 2 段） |
| `empty_static.json` | 全程运动，无静止候选的负例 |

---

## 8. 项目结构

```
app/
  constants.py    单位换算表、重力常量、轴映射取值
  config.py       请求/响应配置模型（Pydantic）与运行时配置
  errors.py       错误类型与统一错误信封
  crypto.py       HMAC-SHA256 签名/验签（真实密码学）
  preprocess.py   单位、轴映射、重复时间戳、时间缺口分段
  detection.py    异常峰值审计、滑窗静止检测、区间合并
  estimator.py    Huber IRLS 稳健回归与模型选择
  pipeline.py     主编排与响应组装（含不可观测参数声明）
  main.py         FastAPI 路由、鉴权中间逻辑、响应签名
scripts/
  make_example_inputs.py   合成数据生成
  signed_request.py        标准库 HMAC 客户端
tests/
  test_pipeline.py  算法测试（14 项）
  test_api.py       HTTP 协议与密码学测试（12 项）
requirements.txt        直接依赖
requirements.lock       全量传递依赖锁定（本次验证环境）
```

## 9. 依赖锁定

- `requirements.lock`：本次实现与验收环境中 `pip freeze` 的全量精确版本
  （numpy 2.5.3、fastapi 0.141.1、pydantic 2.13.5、uvicorn 0.53.0、
  pytest 9.1.1、httpx 0.28.1 等），已在全新 venv 中复现安装并通过全部测试。
- `requirements.txt`：直接依赖与兼容区间。
