# 离散占据栅格融合（occupancy-grid-fusion）

纯后端 Python + NumPy 离线计算库：将二维激光扫描按 **log-odds 占据栅格**模型融合。
只使用合成轨迹与传感器数据，不连接硬件、不含可视化、不依赖 ROS。

## 功能

- 二维激光射线的 log-odds 占据更新
  - 射线**终点单元**按占据更新（`+log_odds_occ`，默认 0.85）
  - 射线**穿越单元**按空闲更新（`+log_odds_free`，默认 -0.4）
  - 未命中（`range >= range_max` 或 NaN/inf）时整条射线按空闲处理
- **概率截断**：log-odds 钳制在 `[clamp_min, clamp_max]`（默认 ±4.0），防止无限累积
- **传感器位姿** `(x, y, theta)` 用于传感器系 → 世界系坐标转换
- Bresenham 直线算法追踪射线穿越的单元
- 越界射线安全裁剪：栅格内部分照常融合，越界部分跳过，不报错
- JSON 入口：读入融合请求，输出占据概率栅格

## 项目结构

```
occupancy_grid/
  geometry.py   # Pose2D、坐标变换、Bresenham
  grid.py       # OccupancyGrid：log-odds 存储、射线更新、截断
  fusion.py     # integrate_scan：整帧扫描融合
main.py         # JSON 入口（CLI）
examples/       # 请求样例（单射线 / 重复观测 / 越界射线）
tests/          # pytest 自动化测试
```

## 安装

```bash
pip install -r requirements.txt   # numpy, pytest
```

## 使用方法

### JSON 入口

```bash
python3 main.py --input examples/request_single_ray.json --output result.json
cat examples/request_single_ray.json | python3 main.py     # stdin/stdout 亦可
```

### 请求格式

```json
{
  "grid":   {"width": 10, "height": 10, "resolution": 1.0, "origin": [0.0, 0.0]},
  "params": {"log_odds_occ": 0.85, "log_odds_free": -0.4,
             "clamp_min": -4.0, "clamp_max": 4.0},
  "scans": [
    {"pose": {"x": 0.5, "y": 0.5, "theta": 0.0},
     "angle_min": 0.0, "angle_increment": 0.0,
     "range_max": 8.0, "ranges": [3.0]}
  ]
}
```

- `grid`：栅格列数/行数、单元边长（米）、左下角世界坐标；`origin` 可省略，默认 `[0,0]`
- `params`：可省略，取默认值
- `scans[]`：每帧扫描含传感器位姿与光束数组；`ranges[i]` 的角度为
  `angle_min + i * angle_increment`（传感器系，弧度）

### 响应格式

```json
{
  "grid": {...},
  "scan_stats": [{"hit": 1, "miss": 0}],
  "probabilities": [[0.401312, ...], ...],
  "log_odds": [[-0.4, ...], ...]
}
```

`probabilities[row][col]`：`row` 对应世界 y，`col` 对应世界 x；0.5 表示未知。

### 作为库调用

```python
from occupancy_grid import OccupancyGrid, Pose2D, integrate_scan

grid = OccupancyGrid(width=10, height=10, resolution=1.0)
integrate_scan(grid, Pose2D(0.5, 0.5, 0.0), ranges=[3.0],
               angle_min=0.0, angle_increment=0.0, range_max=8.0)
print(grid.probabilities())
```

## 算法说明

占据栅格经典模型（Thrun 等）：每个单元维护 log-odds
`l = log(p / (1 - p))`，先验 `l = 0`（p = 0.5）。每次观测做加法更新：

- 终点单元（命中）：`l += log_odds_occ`
- 穿越单元：`l += log_odds_free`
- 每次更新后 `l = clip(l, clamp_min, clamp_max)`

查询时还原概率：`p = 1 - 1 / (1 + exp(l))`。
射线穿越单元由 Bresenham 算法在栅格索引空间枚举，终点单独处理，
因此**障碍后方的单元不会被误标为空闲**。

## 测试

```bash
python3 -m pytest tests/ -v
```

覆盖验收要求：

| 验收项 | 测试 |
|---|---|
| 单射线手算对照 | `test_single_ray.py::test_single_ray_log_odds_hand_computed` 等 |
| 障碍后方不被误标为空闲 | `test_single_ray.py::test_cells_behind_obstacle_not_marked_free` |
| 重复观测累积与截断 | `test_repeated_observations.py`（线性累加、单调上升、钳制、冲突抵消） |
| 越界射线 | `test_out_of_bounds.py`（终点越界、起点越界、斜向裁剪、NaN/inf） |
| JSON 入口 | `test_json_entry.py`（样例请求、校验错误、CLI 端到端） |

## 实际运行记录

环境：Python 3.12.3，NumPy 2.5.3，pytest 9.1.1（Linux x86_64）。

```
$ python3 -m pytest tests/ -v
============================== 24 passed in 0.64s ==============================
```

首次运行 23 通过 / 1 失败：`test_occupancy_probability_grows_with_evidence`
中 6 次命中的 log-odds 累计 5.1 超出截断上限 4.0，末两次概率因钳制相等，
严格单调断言不成立——属测试用例缺陷（实现行为正确）。已拆分为
"未截断区间严格单调"与"饱和于截断上限"两个用例，修正后全部通过。

CLI 单射线样例输出（与手算一致：穿越 -0.4、终点 +0.85、障碍后方保持 0）：

```
$ python3 main.py --input examples/request_single_ray.json
scan_stats: [{'hit': 1, 'miss': 0}]
log_odds row0: [-0.4, -0.4, -0.4, 0.85, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
prob row0: [0.401312, 0.401312, 0.401312, 0.700567, 0.5, 0.5, 0.5, 0.5, 0.5, 0.5]
```

未通过项：无（修正测试缺陷后 24/24 通过）。
