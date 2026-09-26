# diff-odom — 差速轮编码器里程计离线计算库

纯后端 Python + NumPy 库：对差速轮机器人的编码器采样序列做离线里程计积分，
输出位姿轨迹与异常跳变诊断。**仅消费合成/录制数据，不连接硬件、不做可视化、不依赖 ROS。**

## 功能

- 差速轮编码器里程计：轮径、轴距（轮距）、每转计数均可配置
- 计数器回绕处理：支持任意 `[encoder_min, encoder_max]` 量程，正/反向回绕自动修正
- 曲线运动积分：每个采样间隔按**等曲率圆弧**精确积分（直线自动退化），
  对原地旋转与圆弧轨迹无模型误差
- 倒车（负增量）正确处理
- 异常诊断：
  - `timestamp_nonmonotonic`（error）：时间戳非递增
  - `sample_gap`（warning）：采样间隔超阈值，疑似丢样
  - `velocity_jump` / `angular_velocity_jump`（warning）：隐含速度超物理上限，
    疑似打滑、计数跳变或回绕误判
  - `counter_wrap`（info）：回绕事件记录（正常现象）
- JSON 入口：请求/响应均为 JSON，可管道接入其他工具

## 安装与运行

依赖：Python ≥ 3.10，NumPy（测试需 pytest）。

```bash
pip install -r requirements.txt
```

### 命令行

```bash
python3 main.py examples/request_arc.json     # 从文件读请求
cat examples/request_arc.json | python3 main.py   # 或从标准输入
```

输出 JSON 写到标准输出；输入非法时错误写 stderr 并以退出码 1 结束。

### 作为库调用

```python
from diff_odom import RobotParams, run_odometry

params = RobotParams(
    wheel_diameter_m=0.1,   # 轮径
    track_width_m=0.5,      # 轴距（两轮中心距）
    ticks_per_rev=1000,     # 每转计数
    encoder_min=0, encoder_max=65535,
)
result = run_odometry(params,
                      timestamps=[0.0, 0.1, 0.2],
                      left_counts=[0, 1000, 2000],
                      right_counts=[0, 1000, 2000])
print(result["summary"]["final_pose"])
```

## 请求格式

```json
{
  "robot": {
    "wheel_diameter_m": 0.1,
    "track_width_m": 0.5,
    "ticks_per_rev": 1000,
    "encoder_min": 0,
    "encoder_max": 65535,
    "max_linear_velocity_mps": 3.0,
    "max_angular_velocity_rps": 12.0,
    "max_sample_period_s": 0.5
  },
  "initial_pose": {"x_m": 0.0, "y_m": 0.0, "theta_rad": 0.0},
  "samples": [
    {"t": 0.0, "left": 0,    "right": 0},
    {"t": 0.1, "left": 1000, "right": 1000}
  ]
}
```

- `robot` 中后五个字段有默认值（如上），前三个必填；未知键报错。
- `initial_pose` 可省略，默认原点。
- `samples` 至少 1 条；`poses[0]` 即初始位姿。

响应包含 `poses`（每个采样时刻的位姿）、`steps`（每步增量明细）、
`diagnostics`（诊断列表）、`summary`（总里程、终点位姿、诊断计数等）。

## 运动模型

每个采样间隔内假设等曲率运动。设左/右轮位移 `dL`、`dR`（米），轴距 `W`：

```
dθ = (dR - dL) / W,   ds = (dR + dL) / 2
|dθ| < ε  :  x += ds·cosθ,  y += ds·sinθ          （直线）
否则      :  R = ds/dθ
             x += R·(sin(θ+dθ) - sinθ)
             y -= R·(cos(θ+dθ) - cosθ)             （圆弧，精确）
θ 归一化到 (-π, π]
```

计数器回绕：增量对量程 `M = max - min + 1` 取模并折叠到 `[-M/2, M/2)`。
**约束**：单个采样周期内真实计数变化须小于 `M/2`，否则回绕方向无法唯一确定
（采样定理限制，需提高采样率或增大量程）。

## 局限性（重要）

**仅凭编码器无法识别所有打滑。** 编码器测量的是"轮子转了多少"，不是"车体走了多少"：

- 两轮**等量**打滑（如冰面直线空转）：里程计完全无感知，不产出任何诊断
  （`tests/test_pipeline.py::TestSlipLimitation` 固化了这一行为）；
- 单侧打滑有时表现为角速度跳变，可被 `angular_velocity_jump` 捕获，但无法与
  真实的快速转向区分；
- 缓慢、持续的打滑（如沙地）表现为里程计系统性漂移，无任何瞬时特征可检测。

检测打滑需要额外传感器（IMU、视觉、轮速与车体速度交叉验证），超出本库范围。
本库的诊断均为**启发式跳变检测**，不保证检出所有异常，也不保证无误报。

## 项目结构

```
diff_odom/
  config.py       机器人参数与校验
  encoder.py      计数器回绕处理
  integrator.py   等曲率圆弧积分器
  diagnostics.py  跳变/丢样诊断
  pipeline.py     主流程：采样序列 -> 轨迹 + 诊断
  json_io.py      JSON 请求校验与分发
main.py           命令行入口
examples/         合成请求样例（generate_examples.py 重新生成）
tests/            pytest 自动化测试
```

## 测试与实际运行记录

测试覆盖手算验收用例：直行（10×1000 tick = π m）、原地旋转（总转角 0.8π）、
圆弧（R=1 m 四分之一圆 → 终点 (1, 1, π/2)）、回绕（正/反向）、倒车、丢样、
速度跳变、时间戳乱序、输入校验，以及"对称打滑不可检测"的局限性刻画。

实际执行（2026-09-25，Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）：

```
$ python3 -m pytest tests/ -v
============================== 38 passed in 0.20s ==============================
```

首轮运行曾有 2 个失败（`test_reverse`、`TestRotateExample`），原因是测试数据
使用了负计数，而无符号编码器倒车时应向下回绕取模——修正测试数据后全部通过，
库代码未改动。

端到端验证：

```
$ python3 main.py examples/request_arc.json | jq .summary
final_pose: {x_m: 1.0, y_m: 1.0, theta_rad: 1.5707963...}   # 与手算 (1,1,π/2) 一致

$ python3 main.py examples/request_wraparound_reverse.json | jq .summary
final_pose.x_m = -0.0062831...   # 净 -20 tick × π·1e-4 m，正确；counter_wrap_events = 2

$ python3 main.py examples/request_gap_and_jump.json | jq .diagnostics
# 正确产出 sample_gap（dt=1.2s）与 velocity_jump（5.236 m/s）两条 warning

$ echo '{"robot":{}}' | python3 main.py ; echo $?
error: 请求缺少必填字段: samples   # 退出码 1
```

当前无未通过项。
