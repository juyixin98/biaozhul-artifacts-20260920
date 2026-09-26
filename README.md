# 惯性数据预积分子集（IMU 离线积分）

纯后端 Python/NumPy 库：在**已知重力**与**固定偏置**假设下，对 IMU 数据流做离线积分，
输出每个时间戳的姿态（四元数）、速度与位置。提供 JSON 文件入口，仅使用合成轨迹与
传感器数据，不连接硬件、不做可视化、不依赖 ROS。

## 明确不做（范围边界）

- 不做完整 SLAM（无回环、无地图、无因子图优化）。
- 不做偏置在线估计：陀螺/加速度计偏置由请求方以**固定值**给出，积分过程中不更新。
- 不做前端与可视化，不连接任何传感器硬件。

## 模型约定

- 世界系重力向量 `g`，默认 `[0, 0, -9.81]` m/s²。
- 加速度计测量比力：`f = R^T (a_world - g) + b_a`，故 `a_world = R(q) (f_meas - b_a) + g`。
- 陀螺仪测量：`omega_meas = omega_true + b_g`，单位 **rad/s**。
- 姿态四元数格式 `[w, x, y, z]`，表示机体系到世界系的旋转，支持可变时间间隔。
- 积分方法：姿态用区间两端陀螺均值（去偏置）经旋转向量更新四元数；
  速度/位置对旋转到世界系的比力做梯形积分。

## 目录结构

```
imu_preintegration/
  quaternion.py   # 四元数运算（[w,x,y,z]，机体系->世界系）
  validation.py   # 数据流校验（缺样/重复时间/单位可疑等）
  integrator.py   # 离线积分器
  cli.py          # JSON 入口
tests/            # pytest 自动化测试（32 项）
examples/         # 请求样例与生成脚本
```

## 安装与运行

依赖：Python ≥ 3.10，NumPy，pytest（仅测试需要）。

```bash
pip install numpy pytest
```

### JSON 入口

```bash
python -m imu_preintegration.cli examples/request_static.json -o result.json
# 省略 -o 时结果打印到 stdout
```

### 请求格式

```json
{
  "imu": {
    "timestamps": [0.0, 0.01, 0.02],
    "gyro": [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]],
    "accel": [[0.0, 0.0, 9.81], [0.0, 0.0, 9.81], [0.0, 0.0, 9.81]]
  },
  "config": {
    "gravity": [0.0, 0.0, -9.81],
    "gyro_bias": [0.0, 0.0, 0.0],
    "accel_bias": [0.0, 0.0, 0.0],
    "max_dt": 0.05,
    "max_gyro_rad_s": 12.566
  },
  "initial": {
    "orientation": [1.0, 0.0, 0.0, 0.0],
    "velocity": [0.0, 0.0, 0.0],
    "position": [0.0, 0.0, 0.0]
  }
}
```

`config` 与 `initial` 可省略（使用默认值）；未知字段会被拒绝（防止误以为支持
在线偏置估计等未实现功能）。完整样例见 `examples/request_*.json`，可由
`python examples/generate_examples.py` 重新生成。

### 输出格式

```json
{
  "timestamps": [...],
  "orientations": [[w, x, y, z], ...],
  "velocities": [[vx, vy, vz], ...],
  "positions": [[px, py, pz], ...],
  "skipped_intervals": 0,
  "validation": {"ok": true, "issues": []}
}
```

## 数据校验规则

| 情形 | 级别 | code | 行为 |
|------|------|------|------|
| 缺样（相邻 dt > `max_dt`） | warning | `sample_gap` | 记录后继续积分 |
| 重复时间戳（dt = 0） | error | `duplicate_timestamp` | 该区间跳过积分并计数 |
| 时间回退（dt < 0） | error | `non_monotonic_timestamp` | 该区间跳过积分并计数 |
| 角速度幅值超 `max_gyro_rad_s` | warning | `gyro_unit_suspect` | 提示可能误用 deg/s |
| 通道长度不一致 / 样本不足 / 含 NaN | error | `shape_mismatch` / `too_few_samples` / `non_finite` | 记录在报告中 |

error 不阻断积分（结果中如实记录），warning 仅提示。

## 测试

```bash
python -m pytest tests/ -q
```

覆盖：静止、匀速转动（含绕 x 轴重力耦合情形）、恒加速度、可变时间间隔、
固定偏置补偿与未补偿漂移对照、缺样、重复时间、角速度单位错误、JSON 入口。

## 验证记录（实际运行）

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux x86_64。运行日期 2026-09-25。

| 命令 | 结果 |
|------|------|
| `python3 examples/generate_examples.py` | 生成 3 个请求样例，成功 |
| `python3 -m pytest tests/ -q` | **32 passed**，0 失败 |
| `python3 -m imu_preintegration.cli examples/request_static.json` | 末状态 p=[0,0,0], v=[0,0,0], q=[1,0,0,0]，符合理论值 |
| `python3 -m imu_preintegration.cli examples/request_constant_rotation.json` | 0.5 rad/s × 2 s，末姿态 q=[0.8775826, 0, 0, 0.4794255]（yaw=1.0 rad），p/v 为零，符合理论值 |
| `python3 -m imu_preintegration.cli examples/request_constant_accel.json` | a=[1,-0.5,0.2] × 2 s，末 v=[2,-1,0.4]=aT，p=[2,-1,0.4]=½aT²，符合理论值 |
| 缺样场景（删 10 个样本） | 报告 `warning/sample_gap`，积分继续 |
| 重复时间场景 | 报告 `error/duplicate_timestamp`，`skipped_intervals=1` |
| 角速度误用 deg/s | 报告 `warning/gyro_unit_suspect` |

未通过项：无。
