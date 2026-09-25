# trajvel — 运动轨迹速度约束（离线时间参数化）

纯后端 Python + NumPy 库：给定一条折线（路径点序列）与速度/加速度约束，
计算满足约束的时间参数化轨迹。只使用合成轨迹数据做离线计算——
不连接硬件、不做可视化、不依赖 ROS。

## 功能 Hull

```
trajvel/
├── geometry.py        # 折线几何：段长、零长段去重、转角（全部防除零）
├── parameterize.py    # 核心：节点限速 + 前后向传播 + 梯形/三角形段时间
├── sampling.py        # 按固定 dt 采样轨迹（位置/速度/加速度），用于离线验证
└── cli.py             # JSON 入口：python -m trajvel.cli request.json [response.json]
examples/              # 请求样例（直线 / 尖角 / 含零长段）
tests/                 # pytest 自动化测试（33 项）
```

## 算法

1. **节点速度上限**：所有节点不超过 `max_velocity`；中间拐角节点按策略进一步限速：
   - `curvature`（明确曲率模型）：用相邻两段中点弦与切向转角构造内切圆弧，
     曲率 `κ = sin(θ) / chord`，限速 `v ≤ sqrt(a_lat / κ)`；
   - `stop`（停点策略）：每个拐角速度强制为 0。
2. **前向传播**：`v[i+1] = min(limit[i+1], sqrt(v[i]² + 2·a·s[i]))`
3. **后向传播**：`v[i] = min(v[i], sqrt(v[i+1]² + 2·a·s[i]))`
   两遍之后每段满足 `v[i+1]² ≤ v[i]² + 2·a·s[i]`（及反向），段内剖面必然可行。
4. **段时间**：每段用梯形（可达最大速度）或三角形（达不到）速度剖面求最短时间。

**零长段处理**：连续重复路径点（段长 < 1e-12）在参数化前被移除并在
`meta.removed_duplicate_points` 中报告；所有除以段长的位置均有 eps 保护，
零长段时间为 0，不会产生 NaN/Inf。

## 安装与运行

```bash
pip install -r requirements.txt   # numpy, pytest

# JSON 入口（文件 -> 标准输出）
python3 -m trajvel.cli examples/request_line.json

# 文件 -> 文件
python3 -m trajvel.cli examples/request_sharp_corner.json response.json

# 标准输入 -> 标准输出
cat examples/request_zero_length_stop.json | python3 -m trajvel.cli

# 测试
python3 -m pytest -q
```

## 请求格式

```json
{
  "waypoints": [[0.0, 0.0], [4.0, 0.0], [4.0, 3.0]],
  "constraints": {
    "max_velocity": 2.0,
    "max_acceleration": 1.5,
    "max_lateral_acceleration": 1.0
  },
  "corner_strategy": "curvature",
  "start_velocity": 0.0,
  "end_velocity": 0.0,
  "sample_dt": 0.05
}
```

- `waypoints`：N≥2 个等维路径点（2D/3D/任意维均可）。
- `corner_strategy`：`"curvature"`（默认）或 `"stop"`。
- `start_velocity` / `end_velocity`：可选，须在 `[0, max_velocity]` 内，默认 0。
- `sample_dt`：可选；提供时响应中包含按该步长采样的 `samples` 数组。

响应字段：`total_time`、`node_velocities`（各节点速度）、
`segment_durations`、`waypoint_times`（累计时刻）、`corner_velocities`
（拐角限速记录）、`meta`（含零长段移除计数）、可选 `samples`。
请求非法时向 stderr 输出 `{"error": ...}` 并以退出码 2 结束。

## 实测记录（2026-09-25，本机 Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）

### 自动化测试

```
$ python3 -m pytest -q
.................................                                        [100%]
33 passed in 1.55s
```

首次运行曾有 2 项失败（去重逻辑保留尾点导致零长段残留、一处手算总时长错误），
修复后全部通过；当前无未通过项。

### 示例请求实际输出

**直线**（10 m，vmax=2，amax=1，起终速度 0）：

```
total_time: 7.0                  # 加速 2 s + 巡航 3 s + 减速 2 s，与解析解一致
node_velocities: [0.0, 0.0]
max sampled speed: 2.0           # ≤ vmax
max |accel|: 1.0                 # ≤ amax
```

**尖角**（90° 拐角，curvature 策略，a_lat=1.0）：

```
total_time: 4.891814893221081
node_velocities: [0.0, 1.5811388300841898, 0.0]
corner_velocities: {'1': 1.5811388300841898}   # = sqrt(a_lat/κ)，κ=0.4
max sampled speed: 2.0
max |accel|: 1.5
```

**含零长段**（4 个路径点中 2 个重复，stop 策略）：

```
total_time: 5.819124687188632
node_velocities: [0.0, 0.0, 0.0]               # 拐角停点
meta.removed_duplicate_points: 1               # 零长段被移除，无除零、无 NaN
max sampled speed: 1.4715728752538095          # ≤ vmax=1.5
max |accel|: 1.0                               # ≤ amax
```

## 验收对照

| 验收项 | 结果 |
|---|---|
| 直线轨迹速度/加速度上限 | 通过（采样峰值 2.0 / 1.0，不超界） |
| 直线总时长与起终速度 | 通过（7.0 s 与解析解一致，起终均为 0） |
| 尖角按曲率/停点限速 | 通过（曲率策略 v=√(a_lat/κ)；停点策略 v=0） |
| 零长段不除零 | 通过（去重 + eps 保护，输出无 NaN/Inf） |
| 自动化测试 | 33 项全部通过，无未通过项 |
