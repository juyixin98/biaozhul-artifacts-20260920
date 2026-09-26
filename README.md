# 差速里程计积分（differential-odometry）

纯后端 Python + NumPy 离线计算库：由差速轮编码器计数序列积分机器人二维位姿，
并输出异常跳变诊断。只处理合成轨迹与传感器数据——**不连接硬件、不依赖 ROS、
不做可视化、不含前端**。

## 功能

- 差速轮编码器里程计：左右轮计数增量 → 轮位移 → 中心弧长 / 航向变化 → 位姿
- 计数器回绕处理：支持任意模值（如 16 位计数器 65536），正/反向回绕均可修正
- 左右轮径分别配置、轴距配置
- 曲线运动积分（圆弧模型）：恒定轮速下对任意步长精确，直行为其退化情形
- 异常跳变诊断：回绕、时间空洞（丢样）、非单调时间戳、线/角速度突跳
- JSON 入口：文件或 stdin 输入，JSON 输出（stdout 或文件）

## 安装与运行

```bash
pip install -r requirements.txt   # 仅 numpy（运行）与 pytest（测试）

# 运行计算（三选一）
python -m odometry examples/straight.json                 # 结果到 stdout
python -m odometry examples/straight.json -o out.json     # 结果到文件
cat examples/straight.json | python -m odometry           # stdin 输入

# 重新生成 examples/ 下的请求样例
python scripts/generate_examples.py

# 运行测试
python -m pytest tests/ -v
python -m pytest tests/ --cov=odometry --cov-report=term-missing
```

退出码：`0` 成功；`2` 输入非法（错误以 JSON 写到 stderr）。

## 请求格式

```json
{
  "robot": {
    "wheel_diameter_left": 0.16,
    "wheel_diameter_right": 0.16,
    "track_width": 0.4,
    "ticks_per_revolution": 4096,
    "encoder_modulus": 65536
  },
  "diagnostics": {
    "max_linear_velocity": 2.0,
    "max_angular_velocity": 10.0,
    "max_dt": 0.2
  },
  "initial_pose": {"x": 0.0, "y": 0.0, "theta": 0.0},
  "samples": [
    {"t": 0.0,  "left": 65500, "right": 65500},
    {"t": 0.05, "left": 167,   "right": 167}
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `robot.wheel_diameter_left/right` | 是 | 左右轮直径 (m)，可不同 |
| `robot.track_width` | 是 | 两轮中心间距 (m) |
| `robot.ticks_per_revolution` | 是 | 每转计数（含减速比与倍频） |
| `robot.encoder_modulus` | 否 | 计数器模值；省略/`null` 表示计数器不回绕 |
| `diagnostics.*` | 否 | 诊断阈值，缺省 5 m/s、20 rad/s、0.5 s |
| `initial_pose` | 否 | 初始位姿，缺省原点 |
| `samples[].t / left / right` | 是 | 时间戳 (s) 与左右轮计数读数 |

## 响应格式

```json
{
  "poses":       [{"t": 0.0, "x": 0.0, "y": 0.0, "theta": 0.0}, ...],
  "diagnostics": [{"t": 0.05, "flags": ["counter_wrap_left"]}, ...],
  "summary": {
    "sample_count": 41,
    "flagged_samples": 2,
    "total_distance": 0.7999,
    "total_rotation": 0.0,
    "final_pose": {"t": 3.0, "x": 0.2, "y": 0.0, "theta": 0.0}
  }
}
```

诊断标记：`counter_wrap_left` / `counter_wrap_right`（计数器回绕）、
`time_gap`（采样间隔超阈值，疑似丢样）、`non_monotonic_time`（时间戳倒退）、
`velocity_spike` / `angular_spike`（线/角速度超阈值，疑似跳变）。

## 运动模型

单步左、右轮位移 `ds_l, ds_r`（由计数增量 × π·D/每转计数 换算）：

```
ds     = (ds_r + ds_l) / 2         中心弧长
dtheta = (ds_r - ds_l) / L         航向变化，L 为轴距
```

圆弧（等曲率）积分，`theta` 为积分前航向：

```
dtheta ≠ 0:  R = ds / dtheta
    x += R · (sin(theta + dtheta) − sin(theta))
    y −= R · (cos(theta + dtheta) − cos(theta))
dtheta = 0:  x += ds·cos(theta);  y += ds·sin(theta)
```

计数器回绕按“折半取模”修正：`delta = (raw[i] − raw[i−1] + M/2) mod M − M/2`，
在 |单步真实增量| < M/2 时恒成立。

## 局限性（务必阅读）

- **打滑无法仅凭编码器可靠识别。** 编码器只测量轮子转角，测量不到轮地接触
  状态：车轮空转时计数照常增加，机器人并未移动；机器人被外力推移而轮子
  未转时计数毫无变化。这两类情况在编码器数据中与正常运动**不可区分**，
  必须引入 IMU、视觉、激光等外部观测才能检测。本库的诊断仅是启发式异常
  提示（速度突跳、时间异常、回绕事件），不构成打滑或故障的判定。
- 单步真实增量超过计数器模值一半时（欠采样），回绕修正无解，只能依靠
  速度阈值诊断标记。
- 轮径、轴距标定误差与量化误差会随里程累积（里程计固有漂移），本库不做
  误差补偿与滤波融合。

## 项目结构

```
odometry/
  config.py       机器人参数与诊断阈值（dataclass，含校验）
  unwrap.py       计数器回绕修正
  integrate.py    圆弧模型位姿积分
  diagnostics.py  异常跳变诊断
  pipeline.py     端到端流水线（校验→回绕→换算→积分→诊断）
  cli.py          JSON 命令行入口
  synthetic.py    合成编码器数据发生器（仅供测试/示例）
scripts/generate_examples.py   重新生成请求样例
examples/         四个请求样例（直行/原地旋转/圆弧/回绕+倒车+丢样）
tests/            42 项自动化测试
```

## 验收测试记录（实际运行结果）

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux。

手算基准（`tests/test_integrate.py`、`tests/test_pipeline.py`，全部通过）：

| 场景 | 手算解析解 | 数值结果 |
|---|---|---|
| 直行 10 步 × 0.1 m | x=1.0, y=0, θ=0 | 误差 < 1e-12 |
| 原地旋转（左−0.1/右+0.1，L=0.4） | 每步 Δθ=0.5 rad，x,y 不变 | 误差 < 1e-12 |
| 圆弧 R=1 m 四分之一圆 | 终点 (1, 1, π/2) | 误差 < 1e-10 |
| 倒车 10 步 × −0.1 m | x=−1.0 | 误差 < 1e-12 |

回绕 / 倒车 / 丢样（`tests/test_unwrap.py`、`tests/test_pipeline.py`，全部通过）：
模 1000 下 990→10 修正为 +20、10→990 修正为 −20、16 位计数器 65535→2 修正为 +3；
倒车积分 x 为负；时间间隔 0.8 s > 阈值 0.5 s 触发 `time_gap` 且位移不丢失。

最终测试运行：

```
$ python -m pytest tests/ -v
============================== 42 passed in 0.38s ==============================

$ python -m pytest tests/ --cov=odometry --cov-report=term-missing
TOTAL  281 stmts, 24 miss, 91% coverage
```

示例请求实际运行（`python -m odometry examples/<name>.json`）：

| 样例 | 结果 |
|---|---|
| `straight.json`（0.5 m/s × 2 s） | 终点 x=1.00003 m（量化误差 3e-5），0 个标记 |
| `rotate_in_place.json`（1 rad/s × π s） | 终点 θ=−3.133 rad（≈−π，量化误差 0.009 rad），x,y=0 |
| `arc.json`（R=1 m 四分之一圆） | 终点 (1.00001, 1.00415, 1.5751)，理论 (1, 1, π/2) |
| `wrap_reverse_dropout.json` | 终点 x=0.20003 m（前进 0.5 m 再倒车 0.3 m），正确标记 `counter_wrap_*` 与 `time_gap` |

未通过项与处理（如实记录）：开发过程中首轮运行有 3 项失败，均为**测试用例
本身的期望值错误**，库实现无误，修正测试后全部通过：

1. `test_counter_wrap_end_to_end`：期望值按“每步 +20 tick”计算，但用例数据
   第二步实际为 +100 tick；已将数据改为 990→10→30（每步 +20）。
2. `test_velocity_spike_flagged`：跳变量 50000 tick 恰为模 1000 的整倍数，
   被回绕修正正确归零（欠采样不可恢复情形）；已改用无回绕计数器配置测试突跳。
3. `test_unequal_wheel_diameters`：断言方向写反——右轮更大应向左偏航（θ>0）；
   已修正断言与注释。

当前无未通过项。
