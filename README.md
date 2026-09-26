# 运动轨迹速度约束（离线时间参数化）

纯后端 Python + NumPy 库：给定折线路径与动力学约束，计算满足
**速度上限、加速度/减速度上限、起终速度** 的时间最优
（bang-coast-bang）时间参数化。只使用合成轨迹与合成传感器数据，
不连接硬件、不做可视化、不依赖 ROS。

## 功能

- 沿折线的时间参数化（梯形/三角形速度曲线，闭式求解）
- 节点速度**前向 + 后向传播**取交，保证全局可行
- 拐角限速两种明确策略：
  - `stop`：内部节点（或显式停点）速度强制为 0
  - `curvature`：按显式曲率 κ（或半径 R=1/κ）限速
    `v ≤ sqrt(a_lat_max / κ) = sqrt(a_lat_max · R)`
  - 折返尖点（转角 ≥ `cusp_angle_deg`，默认 170°）任何策略下强制停车
- **零长段安全**：相邻重复点在规划前折叠；内部所有除以段长的位置
  对零长段短路，不产生除零或 NaN
- 不可行请求显式报错（如路径太短无法在减速度上限内从指定初速刹停），
  绝不静默篡改用户指定的起终速度
- 独立数值校验：对规划结果稠密采样，用中心差分复核速度/加速度上限
- 合成里程计仿真（高斯噪声）与误差统计，用于离线自检
- JSON 命令行入口

## 安装与运行

无第三方运行时依赖之外的安装步骤，仅需 Python ≥ 3.10 与 NumPy：

```bash
pip install numpy pytest
```

运行示例（直线）：

```bash
python3 -m trajectory_planning.json_entry examples/request_straight.json --pretty
```

从标准输入读取：

```bash
cat examples/request_sharp_corner_stop.json | python3 -m trajectory_planning.json_entry
```

运行测试：

```bash
python3 -m pytest tests/ -q
```

## 请求格式

```json
{
  "points": [[0, 0], [2, 0], [2, 2]],
  "dynamics": {
    "v_max": 1.0,
    "a_max": 0.5,
    "d_max": 0.5,
    "a_lat_max": 0.6,
    "v_start": 0.0,
    "v_end": 0.0
  },
  "corner": {
    "strategy": "curvature",
    "radius": 0.5,
    "stop_nodes": [],
    "cusp_angle_deg": 170.0
  },
  "sample_dt": 0.05,
  "synthetic_sensor": {
    "dt": 0.02, "position_noise_std": 0.001, "speed_noise_std": 0.01, "seed": 7
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `points` | 是 | 折线节点坐标（2D/3D 均可），相邻重复点自动折叠 |
| `dynamics.v_max` | 是 | 速度上限 (m/s) |
| `dynamics.a_max` | 是 | 加速度上限 (m/s²) |
| `dynamics.d_max` | 否 | 减速度上限，缺省等于 `a_max` |
| `dynamics.a_lat_max` | 曲率策略必填 | 横向加速度上限 (m/s²) |
| `dynamics.v_start` / `v_end` | 否 | 起/终速度，缺省 0；不可行时报错而非篡改 |
| `corner.strategy` | 否 | `stop`（默认）或 `curvature` |
| `corner.radius` / `corner.curvature` | 曲率策略必填其一 | 显式圆角半径或曲率，二选一 |
| `corner.node_curvatures` | 否 | 逐节点曲率覆盖（长度 = 去重后节点数） |
| `corner.stop_nodes` | 否 | 强制停车的节点编号 |
| `corner.cusp_angle_deg` | 否 | 折返尖点判定角，默认 170 |
| `sample_dt` | 否 | 附加稠密采样的步长（秒） |
| `synthetic_sensor` | 否 | 附加带噪合成里程计读数 |

## 响应格式

成功时 `status: "ok"`，包含：

- `summary`：节点数、路径长、**总时长**、起终速度
- `node_speeds`：传播后的各节点速度
- `corner_details`：每个拐角的转角、策略、限速值与判定依据
- `segments`：每段的加速/巡航/制动时间分配
- `validation`：独立数值校验报告（最大速度/加/减速度、各项违反量）
- `samples` / `synthetic_odometry`：按请求附加

失败时 `status: "error"`，含 `error_type` 与中文 `message`；
CLI 退出码：0 成功，1 规划/参数错误，2 请求文件无法读取。

## 算法说明

1. **预处理**：折叠相邻重复点（容差 1e-12 m），得到无零长段的折线。
2. **拐角限值**：按策略给每个内部节点一个速度上限
   （停点为 0；曲率策略为 `sqrt(a_lat_max/κ)`；尖点强制 0）。
3. **前后向传播**：
   `v²(s+L) ≤ v²(s) + 2·a_max·L`（前向）、
   `v²(s) ≤ v²(s+L) + 2·d_max·L`（后向），
   与拐角上限取交，得到时间最优节点速度。
4. **可行性检查**：传播若压低了用户指定的起/终速度，抛
   `InfeasibleTrajectory`。
5. **分段规划**：每段闭式求解梯形（有巡航）或三角形（无巡航）
   速度曲线，切换条件为三角形峰值速度是否超过 `v_max`。
6. **校验**：独立采样 + 中心差分复核全部约束。

## 项目结构

```
trajectory_planning/
  path.py        折线几何、重复点折叠、零长段安全
  limits.py      动力学参数与拐角策略
  kinematics.py  前后向传播、单段梯形规划、不可行异常
  profile.py     轨迹档案、采样、独立数值校验
  synthetic.py   合成里程计仿真与误差统计
  planner.py     请求解析与规划编排
  json_entry.py  JSON 命令行入口
examples/        请求样例（直线/尖角停点/曲率/零长段/不可行）
tests/           pytest 自动化测试（43 项）
RUNLOG.md        实际运行命令与结果记录
```
