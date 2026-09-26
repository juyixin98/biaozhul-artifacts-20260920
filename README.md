# CCD — 连续碰撞检测（Continuous Collision Detection）

纯 Python + NumPy 的离线计算库：求解二维圆形机器人沿线性轨迹与移动圆形障碍物的**最早接触时间**。闭式求解二次方程，**不使用任何离散采样**，因此任意高速相对运动（离散采样会漏检的穿越/tunneling）都能被精确捕获。不连接硬件、不做可视化、不依赖 ROS。

## 原理

机器人与障碍物的圆心做匀速直线运动。在障碍物参考系中，相对位置为

```
d(t) = d0 + v t,  d0 = p_r0 - p_o0,  v = v_r - v_o
```

接触条件为 |d(t)| = R（R = 两圆半径和），即

```
f(t) = (v·v)t² + 2(d0·v)t + (d0·d0 - R²) 即 a t² + b t + c = 0
a = v·v,  b = 2(d0·v),  c = d0·d0 - R²
```

最早接触时间 = 闭区间窗口内最小的根。求根使用数值稳定形式（`q = -(b + sign(b)·√disc)/2`，避免相消误差）。

## 区间闭合规则（区间语义）

- 时间窗口 `[t_min, t_max]` **两端闭合**：根恰好落在端点上（容差 1e-9 内）计为接触。
- **相切**（判别式 = 0，单根）：计为接触，状态 `tangent`。
- **初始重叠**（t_min 时刻圆心距 < 半径和）：接触时间 = t_min，状态 `already_overlapping`。
- **初始相切**（t_min 时刻距离恰为 R）：接触时间 = t_min，状态 `tangent`。
- **相同速度**（a = 0）：相对位置恒定，要么 t_min 已接触，要么永不接触。
- 求解器内部不做任何采样，答案就是二次方程的精确根。

## 安装与运行

```bash
pip install numpy pytest   # 唯一运行时依赖是 numpy
python -m pytest           # 运行自动化测试
python -m ccd examples/head_on.json        # 从文件读请求
python -m ccd < examples/head_on.json       # 从 stdin 读请求
```

## 请求格式（JSON 入口）

```json
{
  "time_window": [0.0, 10.0],
  "robot":    {"position": [0.0, 0.0], "velocity": [1.0, 0.0], "radius": 1.0},
  "obstacles": [
    {"id": "obs-1", "position": [10.0, 0.0], "velocity": [-1.0, 0.0], "radius": 1.0}
  ]
}
```

- `time_window` 可选，默认 `[0, +∞)`；两端闭合。
- `position` / `velocity` 为 2 元素数组，`radius` 为正数。
- 响应含每个障碍物的结果（`status`, `time`, `distance_at_contact`）及全局最早接触 `earliest`。
- `status` ∈ `collision` / `tangent` / `already_overlapping` / `no_collision`。

## 验收场景与手算验证

以下每个场景都有对应的手算根和自动化测试（`tests/`）及可运行样例（`examples/`）。

### 1. 对撞（head_on.json）— 手算验证

机器人 (0,0) v=(1,0) r=1，障碍物 (10,0) v=(−1,0) r=1。
d₀ = (−10,0)，v = (2,0)，R = 2，f(t) = 4t² − 40t + 96 = 0 → t = 4 或 6，最早接触 **t = 4**。

实测输出（`python -m ccd examples/head_on.json`）：

```json
{"earliest": {"obstacle_id": "wall-runner", "status": "collision", "time": 4.0, "distance_at_contact": 2.0}, ...}
```

### 2. 相切（tangent.json）

机器人 (0,0) v=(1,0) r=1，障碍物 (5,2) r=1。
f(t) = (t−5)² = 0 双根 t=5（只接触不穿透）→ 状态 `tangent`，时间 **5.0**。

### 3. 初始重叠（initial_overlap.json）

两静止圆心距 1.5 < 2 → `already_overlapping`，时间 **0.0**。

### 4. 相同速度（same_velocity.json）

v 相同，相对位置恒定，圆心距 5 > 2 → `no_collision`（若初始重叠则报 `already_overlapping`，见测试）。

### 5. 高速穿越（high_speed.json）

机器人 (0,0) v=(1000,0) r=0.5，障碍物 (500, 0.5) r=0.5，R = 1。
f(t) = (1000t − 500)² + 0.25 − 1 = 0 → t = (500 − √0.75)/1000 ≈ **0.4991339745962156**（实测输出与手算一致）。
重叠窗口宽度仅 2√0.75/1000 ≈ 0.00173 s，任何 dt 大于此的离散采样都会漏检；闭式求解不受影响。

### 6. 不相交（miss.json）

最近距离 5 > 半径和 2 → `no_collision`。

## 实际运行记录（如实记录）

运行环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1，Linux。

```
$ python3 -m pytest -v
collected 23 items
tests/test_api.py::test_solve_request_head_on PASSED
tests/test_api.py::test_solve_request_picks_earliest_obstacle PASSED
tests/test_api.py::test_solve_request_no_time_window_defaults_to_infinite PASSED
tests/test_api.py::test_solve_request_no_collision PASSED
tests/test_api.py::test_invalid_requests_raise_value_error PASSED
tests/test_api.py::test_cli_roundtrip PASSED
tests/test_api.py::test_cli_invalid_input_exits_2 PASSED
tests/test_solver.py::test_head_on_quadratic_coefficients PASSED
tests/test_solver.py::test_head_on_earliest_contact PASSED
tests/test_solver.py::test_head_on_window_excludes_entry_root PASSED
tests/test_solver.py::test_head_root_on_closed_window_boundary PASSED
tests/test_solver.py::test_head_on_window_start_inside_overlap PASSED
tests/test_solver.py::test_head_on_window_start_before_entry PASSED
tests/test_solver.py::test_tangent_contact PASSED
tests/test_solver.py::test_tangent_root_outside_window PASSED
tests/test_solver.py::test_initial_overlap_static PASSED
tests/test_solver.py::test_initial_touching_at_start_is_tangent PASSED
tests/test_solver.py::test_same_velocity_no_contact PASSED
tests/test_solver.py::test_same_velocity_overlapping PASSED
tests/test_solver.py::test_high_speed_tunneling PASSED
tests/test_solver.py::test_miss PASSED
tests/test_solver.py::test_invalid_radius PASSED
tests/test_solver.py::test_invalid_window PASSED
23 passed in 0.57s
```

**未通过项：无（23/23 通过）。**

`python -m ccd examples/<name>.json` 全部按预期输出（见上文各场景）。

## 项目结构

```
ccd/
  __init__.py     # 包导出
  models.py       # CircleBody / ContactResult / ContactStatus
  solver.py       # 闭式二次方程求解器（核心，无采样）
  api.py          # JSON 请求/响应入口
  __main__.py     # CLI：python -m ccd [request.json]
examples/          # 6 个验收场景请求样例
tests/             # pytest 套件（手算根验证 + API + CLI）
```

## 边界情况处理

- 判别式容差：相对容差 1e-9，小于此视为相切双根。
- 窗口边界容差：根在窗口端点 1e-9 内会被收拢到端点。
- 输入校验：半径必须为正有限数，位置/速度必须为有限 2 向量，时间窗口必须满足 `0 ≤ t_min ≤ t_max`；非法输入返回错误（CLI 退出码 2）。
- 数值稳定求根：使用 `q = −(b + sign(b)√disc)/2` 形式避免相消误差。
