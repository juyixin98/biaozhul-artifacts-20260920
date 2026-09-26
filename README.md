# 2D 激光扫描匹配（Point-to-Point ICP）

纯后端离线计算库：用 Python + NumPy 实现二维点到点 ICP（Iterative Closest
Point）位姿估计。只使用合成轨迹与传感器数据，不连接硬件、不做可视化、
不依赖 ROS。

## 功能

- **点到点 ICP 二维位姿估计**：SE(2) 位姿 `(x, y, theta)`，每轮迭代用
  SVD（Kabsch）闭式求解最优刚体变换增量。
- **明确的最近邻与离群拒绝**：暴力最近邻搜索（分块计算，内存有界）；
  先按 `max_correspondence_distance` 距离门限剔除，再按 `trim_ratio`
  只保留距离最小的一部分匹配对。
- **迭代诊断**：每轮输出残差（RMSE）、匹配对数、增量平移/旋转量；
  收敛状态为 `converged` / `max_iterations` / `failed` 之一。
- **退化检测**：当最终匹配点近似共线（如直墙场景），协方差特征值比
  低于阈值时返回 `degenerate: true` 及不可观测方向，结果应视为不确定。
- **JSON 入口**：命令行读取 JSON 请求（文件或标准输入），输出 JSON
  结果信封。

## 安装

```bash
pip install -r requirements.txt   # numpy, pytest
```

## 快速开始

### 作为库调用

```python
import numpy as np
from scan_matching import SE2Pose, ICPParams, icp
from scan_matching.synthetic import generate_scene, generate_scan

rng = np.random.default_rng(42)
scene = generate_scene("room")
target = generate_scan(scene, SE2Pose(0.0, 0.0, 0.0), rng=rng)
source = generate_scan(scene, SE2Pose(0.3, -0.15, 0.08), rng=rng)

result = icp(source, target, params=ICPParams(trim_ratio=0.9))
print(result.pose, result.status, result.residual, result.degenerate)
```

### JSON 命令行入口

```bash
python -m scan_matching.cli examples/request_scenario_room.json
cat examples/request_explicit.json | python -m scan_matching.cli -
```

请求格式（二选一提供点云）：

```json
{
  "source": [[x, y], "..."],
  "target": [[x, y], "..."],
  "initial_pose": {"x": 0.0, "y": 0.0, "theta": 0.0},
  "params": {"max_iterations": 50, "trim_ratio": 0.9}
}
```

或使用合成场景描述（自动生成点云并附真值）：

```json
{
  "scenario": {
    "scene": "room",
    "source_pose": {"x": 0.3, "y": -0.15, "theta": 0.08},
    "target_pose": {"x": 0.0, "y": 0.0, "theta": 0.0},
    "noise_sigma": 0.01,
    "outliers": 30,
    "seed": 42
  }
}
```

响应信封：成功 `{"success": true, "result": {...}}`；失败
`{"success": false, "error": {"type": "...", "message": "..."}}`（退出码 1）。
`result` 包含估计位姿、收敛状态、逐轮迭代记录、残差、退化标志；
场景模式下还包含 `ground_truth` 与 `pose_error`。

### 参数（`params`，均可选）

| 参数 | 默认值 | 含义 |
|---|---|---|
| `max_iterations` | 50 | 最大迭代轮数 |
| `translation_tolerance` | 1e-5 | 增量平移收敛阈值（m） |
| `rotation_tolerance` | 1e-5 | 增量旋转收敛阈值（rad） |
| `residual_tolerance` | 1e-8 | 残差变化收敛阈值 |
| `max_correspondence_distance` | 2.0 | 匹配距离门限（m），超出即离群 |
| `trim_ratio` | 1.0 | 每轮保留的最佳匹配对比例 |
| `degeneracy_ratio_threshold` | 1e-3 | 共线判定特征值比阈值 |

## 算法说明

每轮迭代：

1. 用当前位姿变换 source 点云；
2. 对每个变换后点暴力搜索 target 中最近邻（分块矩阵运算）；
3. 离群拒绝：距离门限 + 按比例截尾；
4. 对保留的匹配对用 SVD 闭式求解 SE(2) 增量（保证旋转行列式为 +1）；
5. 增量或残差变化低于阈值则判定收敛。

退化检测：对最终匹配点计算 2×2 协方差，特征值比
`lambda_min / lambda_max < degeneracy_ratio_threshold` 时判定共线退化，
不可观测方向（沿直线方向，即最大特征值对应特征向量）通过
`degenerate_direction` 返回。

## 实测运行记录（2026-09-25，Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1）

```bash
$ python3 -m pytest tests/ -q
28 passed in 4.72s        # 语句覆盖率 97%
```

```bash
$ python3 -m scan_matching.cli examples/request_scenario_room.json
# converged，25 轮迭代；残差 0.2454 → 0.0174
# 估计 (0.30041, -0.15067, 0.07994) vs 真值 (0.3, -0.15, 0.08)
# pose_error: translation 0.00079 m, rotation 5.7e-05 rad（含 30 个离群点、部分重叠）

$ python3 -m scan_matching.cli examples/request_scenario_line_degenerate.json
# converged，degenerate: true，degenerate_direction ≈ (-1.0, 0.0)（沿直墙）
# y 恢复正确（0.2003 vs 0.2），x 不可观测（0.008 vs 真值 0.4）——
# 结果按约定标记为不确定

$ cat examples/request_explicit.json | python3 -m scan_matching.cli -
# converged，3 轮；精确恢复平移 (0.5, 0.2)，残差 ~4.1e-16
```

测试覆盖验收项：已知变换恢复、离群点注入、部分重叠、直线退化返回
不确定、错误初值陷入局部最优（`TestLocalOptimum`，180° 初值偏差下
收敛到错误位姿——点到点 ICP 是局部方法，需要合理初值）。

## 已知限制

- 点到点 ICP 是局部优化：初值偏差过大（如旋转 180°）会收敛到局部
  最优，需由上游（里程计等）提供合理初值。
- 退化检测基于匹配点共线性，可识别直线墙场景；双平行线走廊这类
  "点分布不共线但约束仍退化"的场景不在检测范围内。
- 最近邻为暴力 O(N·M) 实现，合成数据规模（千点级）足够；大规模点云
  需换 KD 树。

## 项目结构

```
scan_matching/
  geometry.py    # SE(2) 位姿：组合、求逆、变换
  icp.py         # 点到点 ICP 主循环、最近邻、离群拒绝、退化检测
  synthetic.py   # 合成场景与扫描生成（房间/直线/走廊）
  cli.py         # JSON 命令行入口
examples/        # 请求样例（场景模式 ×2、显式点云 ×1）
tests/           # pytest 自动化测试（28 项）
```
