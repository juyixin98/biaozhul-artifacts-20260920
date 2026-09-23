# IMU 偏置估计服务（离线静止段识别 + 陀螺零偏稳健估计）

纯后端服务（Python + NumPy + FastAPI）。输入加速度计、陀螺仪采样与时间戳，
识别静止候选区间，并**仅**在静止样本上稳健估计陀螺零偏；温漂、运动、
异常峰值与时间异常均显式处理，**不可观测参数明确标注为不可识别，绝不谎报
已求解**。

## 它能做什么 / 不能做什么

| 参数 | 仅静止数据是否可识别 | 说明 |
| --- | --- | --- |
| 陀螺零偏（零速率输出） | ✅ 可估计 | 静止时真实角速度为零，与姿态无关；中位数 + MAD 稳健估计 |
| 陀螺温漂系数（rad/s/°C） | ⚠️ 有条件 | 需要 ≥3 个静止区间且温度跨度足够，否则显式标记不可识别 |
| 陀螺随时间漂移（rad/s/s） | ⚠️ 有条件 | 需要 ≥3 个静止区间且时间跨度足够 |
| 加速度计零偏 | ❌ 不可分离 | 静态比力 = R·g + bₐ，姿态 R 未知；只输出重力向量/幅值残差 |
| 刻度系数、轴间非正交 | ❌ 不可识别 | 静止时激励恒定，无独立轴激励 |
| 陀螺噪声密度/Allan 方差 | ❌ 不完整 | 仅给出窗口内稳健 MAD 噪声代理量 |
| 陀螺 g-敏感度 | ❌ 不可识别 | 只有单一重力方向 |

## 算法（全部内部使用 SI 单位：s、m/s²、rad/s、°C）

1. **预处理**：非有限值拒绝；时间排序（报告乱序样本数）；重复时间戳按
   `first` / `mean` / `error` 策略处理。
2. **时间缺口分段**：`dt > min(max_gap_sec, gap_factor × median_dt)` 处分段；
   静止检测在每个连续段内独立进行，跨段候选在“偏置恒定”假设下合并。
3. **滑动窗口静止判定**（默认 1 s、50% 重叠），窗口必须**同时**满足：
   - 加速度计最大轴方差 ≤ `accel_variance_thresh`；
   - 陀螺最大轴方差 ≤ `gyro_variance_thresh`；
   - 平均比力幅值在重力 ± `gravity_tolerance` 内；
   - 平均角速度范数 ≤ `gyro_mean_thresh`。
   任一条件不过即按原因计入拒绝统计——**运动段与异常峰值永远进不了偏置池**。
4. **区间合并**：相邻静止游程间隔 ≤ `bridge_gap_sec` 合并为一个候选区间
   报告（中间被拒绝样本不进入估计池）；短于 `min_candidate_duration_sec`
   的区间丢弃。
5. **稳健估计**：候选样本逐轴取中位数为零偏，MAD 为稳健尺度；给出标准误、
   95% 置信区间、逐轴 high/medium/low/none 置信等级。
6. **区间一致性**：以区间中位数的加权 χ² 检验检测温漂/慢变；不一致时降级
   置信度并给出告警，而不是把漂移平均掉假装是一个恒定零偏。
7. **漂移模型**：区间中位数对温度/时间做加权线性回归（含斜率标准误、R²），
   数据不足时显式说明原因。
8. **异常峰值**：Hampel 风格双侧邻域检测，只报告“两侧邻域都安静的孤立离群
   点”——持续运动和运动边界不会被误标。

## 目录结构

```
imu_bias_estimator/   核心包
  units.py            显式单位换算
  models.py           Pydantic 请求/响应协议
  preprocess.py       校验、单位转换、时间去重、缺口分段
  estimator.py        滑窗检测、区间合并、稳健估计、漂移拟合、尖峰检测
  pipeline.py         端到端装配 + 可观测性声明
  app.py              FastAPI 路由
tests/                19 个自动化测试（算法 + HTTP）
examples/             合成示例数据生成器与样例请求
scripts/run_offline.py  离线 CLI
requirements.lock     完整锁定的依赖版本
```

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock   # 或 requirements.txt
.venv/bin/uvicorn imu_bias_estimator.app:app --host 127.0.0.1 --port 8000
```

启动后交互式文档：<http://127.0.0.1:8000/docs>

### 接口

- `GET /health`
- `GET /api/v1/observability` — 可观测性声明
- `POST /api/v1/estimate` — 主体服务

请求字段（全部阈值均可覆盖，详见 `models.DetectorConfig`）：

```json
{
  "timestamps": [0.0, 0.01, ...],
  "accelerometer": [[0.0, 0.0, 9.806], ...],
  "gyroscope": [[0.001, -0.002, 0.0], ...],
  "temperature": [25.1, ...],
  "units": {"time": "s", "acceleration": "m/s^2",
            "angular_velocity": "rad/s", "temperature": "c"},
  "axes": {"accelerometer": ["x","y","z"], "gyroscope": ["x","y","z"]},
  "config": {"window_sec": 1.0, "gyro_variance_thresh": 0.0002},
  "time_repeat_policy": "first"
}
```

支持单位：时间 `s/ms/us/ns`；加速度 `m/s^2/g/mg`；角速度
`rad/s / deg/s(=dps) / rad/hr / deg/hr`；温度 `c/k/f`。

## 示例与验收命令

```bash
# 1) 生成合成示例（含温漂、突发运动、时间缺口、尖峰、重复时间戳）
.venv/bin/python examples/generate_sample.py

# 2) 离线运行并查看报告
.venv/bin/python scripts/run_offline.py examples/sample_request.json out/report.json

# 3) 自动化测试
.venv/bin/python -m pytest -q

# 4) HTTP 验收（先启动服务）
curl -s http://127.0.0.1:8000/health
curl -s -X POST http://127.0.0.1:8000/api/v1/estimate \
     -H 'Content-Type: application/json' \
     --data-binary @examples/sample_request.json | python3 -m json.tool | head -60
```

示例数据（60 s @100 Hz）预期：检测到 4 个候选区间、1 个 5 s 时间缺口、
1 个重复时间戳、1 个陀螺尖峰（t≈40 s）；区间 χ² 检验报告 `inconsistent`
（合成数据注入了 0.002 rad/s/°C 的温漂），温度回归恢复注入斜率，逐轴置信
等级相应降级——这是**如实报告**，不是估计失败。

## 输出要点

- `candidates[]`：区间（所属段、起止索引/时间、样本数、温度均值、比力均值
  与重力残差、陀螺中位数/MAD/RMSE/最大残差）。
- `gyroscope_bias`：rad/s 与 deg/s 双单位、95% CI、置信等级、加权 χ²、
  区间一致性。
- `residuals`：候选内陀螺残差 RMSE/最大绝对值、候选间最大角速度范数、
  各区间重力残差。
- `drift_temperature` / `drift_time`：拟合斜率（带标准误）或显式不可识别原因。
- `anomalies`：重复时间戳、乱序、缺口、尖峰、突发运动（候选外最大角速率）。
- `observability`：逐参数可观测性与理由。
- 无任何静止数据时返回 HTTP 200 + `"status": "no_stationary_data"`，
  `gyroscope_bias` 为 `null`，不编造数字。
