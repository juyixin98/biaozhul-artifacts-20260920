# 运动碰撞连续检测（CCD）离线计算库

纯后端 Python + NumPy 库：求解**二维圆形机器人**沿**线性轨迹**与**移动圆障碍**
的连续碰撞时间（最早接触时刻）。输入仅为合成轨迹与合成传感器采样，
不连接硬件、不含可视化、不依赖 ROS。

## 原理：解析求根，不做离散采样

机器人与障碍的圆心随时间线性运动：

```
p_R(t) = c_R + v_R·t        p_O(t) = c_O + v_O·t
```

相对位置 `d(t) = (c_R − c_O) + (v_R − v_O)·t`。两圆接触（含相切）当且仅当
`|d(t)|² ≤ (r_R + r_O)²`，展开为关于 t 的二次方程：

```
a·t² + b·t + c = 0
a = |v_rel|²,  b = 2·d0·v_rel,  c = |d0|² − R²   (R = r_R + r_O)
```

接触时间区间即抛物线 ≤ 0 的解集，由 `collision_detection/quadratic.py`
用**数值稳定求根公式**（q 公式，避免 `−b ± √D` 抵消）解析求得。
全程无时间离散化，因此任意高速穿越（tunneling）都不会漏检——
测试中用 dt=0.01 的采样反证了离散方法会漏掉宽度仅 1.25e-4 s 的接触窗口。

## 区间闭合规则（明确约定）

1. 接触区间 `[t_enter, t_exit]` 为**闭区间**，端点（恰好接触瞬间）计为碰撞；
2. **相切**（判别式 = 0，二重根）计为碰撞，`t_enter == t_exit`；
3. **初始重叠**（窗口起点已满足 `|d0| < R`）计为碰撞，`t_enter` 取窗口起点；
4. 时间窗口 `[t_start, t_end]` 两端均闭：接触时刻恰等于端点时计入窗口；
5. 相对速度为零（a = 0）时间距恒定：当前重叠则整窗碰撞，否则不碰撞。

## 安装与运行

```bash
pip install -r requirements.txt   # 仅 numpy

# 运行一个请求样例（结果打印到标准输出）
python3 -m collision_detection.cli examples/request_high_speed.json

# 输出到文件
python3 -m collision_detection.cli examples/request_tangent.json -o response.json

# 重新生成合成传感器样例（固定随机种子，可复现）
python3 examples/generate_sensor_request.py

# 运行全部测试
python3 -m unittest discover -s tests -v
```

## 请求格式（JSON 入口）

```json
{
  "time_window": [0.0, 1.0],
  "robot":     { "center": [0, 0], "velocity": [1, 0], "radius": 1.0 },
  "obstacles": [
    { "center": [5, 0], "velocity": [-1, 0], "radius": 1.0 },
    { "radius": 1.0, "samples": [[0, 6, 0.5], [1, 5, 0.5], [2, 4, 0.5]] }
  ]
}
```

- `time_window` 可选，默认 `[0.0, 1.0]`；
- 运动体支持两种描述（可混用，但同一体不可同时给）：
  - **直接运动学参数**：`center` + `velocity` + `radius`；
  - **合成传感器采样**：`samples`（`[[t, x, y], ...]`，≥2 点）+ `radius`，
    库内最小二乘拟合为线性轨迹后再做解析碰撞分析；
- 响应为统一信封 `{success, data, error, metadata}`，`data.obstacles[i].analysis`
  含 `collides / kind / t_enter / t_exit / coefficients / roots`，
  `data.earliest_collision` 给出全体障碍中的最早接触。

`kind` 取值：`none`（无碰撞）、`overlap`（初始重叠）、
`crossing`（进入并穿出）、`tangent`（相切）。

## 验收场景与手算对照

| 样例 | 场景 | 手算 | 实测 |
|---|---|---|---|
| `request_tangent.json` | 相切 | a=4,b=−16,c=16，D=0，二重根 t=2 | t_enter=2.0, kind=tangent |
| `request_initial_overlap.json` | 初始重叠 | a=1,b=−6,c=−7，根 −1,7，覆盖 t=0 | t_enter=0.0, kind=overlap |
| `request_same_velocity.json` | 相同速度 | a=0：障碍0 间距 5>R=2 永不碰；障碍1 间距 1.5<2 整窗重叠 | 障碍0 none，障碍1 overlap |
| `request_high_speed.json` | 高速穿越 | a=1e6,b=−1001000,c=250500.2461，D=15600，t=(1001000±√15600)/2e6 | t_enter≈0.50043755, kind=crossing |
| `request_sensor_fit.json` | 传感器拟合 | 真值 t=(24−√60)/8≈2.032 | t_enter≈2.025（噪声拟合偏差 <0.05） |

每个场景的手算过程同时写在对应测试的 docstring 中
（`tests/test_collision.py`），断言与手算根逐一相等。

## 项目结构

```
collision_detection/
  quadratic.py    # 稳定二次方程求根（解析解核心）
  models.py       # CircleBody 线性运动圆盘
  collision.py    # CCD 核心：接触区间、闭合规则、多障碍
  trajectory.py   # 传感器采样 → 最小二乘线性轨迹拟合
  io_json.py      # 请求校验 / 响应信封
  cli.py          # python -m collision_detection.cli
examples/         # 五个验收场景请求样例 + 传感器样例生成器
tests/            # 40 个 unittest，全部含手算对照
```

## 实测记录

环境：Python 3.12.3，NumPy 2.5.3，Linux。

- `python3 -m unittest discover -s tests` → **40 tests, OK**（曾有一次失败：
  传感器样例期望值手算笔误 b=−22 写成正确值 b=−24 之前，修正注释与期望后通过；
  库代码未改动）；
- 五个样例 CLI 实跑结果见上表“实测”列，全部与手算一致；
- 未通过项：无。
