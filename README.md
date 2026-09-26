# icp2d — 二维激光扫描匹配（点到点 ICP）

纯后端离线计算库：给定两帧二维点云（参考帧 `target` 与待配准帧 `source`），
估计把 `source` 对齐到 `target` 的刚体变换位姿 `(x, y, theta)`。

- 纯 Python + NumPy，无 ROS、无硬件、无可视化、无前端
- 点到点 ICP：最近邻关联 → 显式离群拒绝 → 线性化 SE(2) 高斯-牛顿步，迭代至收敛
- 每次迭代记录残差（RMSE）与步长，输出收敛状态
- 退化场景（如长直走廊）**返回不确定标记**，而不是输出一个看似自信的位姿
- JSON 请求/响应入口（文件或标准输入），另附 Python API
- 全部测试数据为合成轨迹与传感器数据（矩形房间、直墙、噪声、离群点、部分重叠）

## 安装与运行

要求 Python ≥ 3.10 与 NumPy ≥ 1.24（测试需要 pytest）：

```bash
pip install numpy pytest        # 或: pip install -e .[test]
```

运行测试（在仓库根目录）：

```bash
python3 -m pytest -q
```

JSON 入口：

```bash
python3 -m icp2d examples/01_known_transform.json     # 从文件读请求
python3 -m icp2d - < examples/01_known_transform.json # 从标准输入读
```

退出码：`0` = 求解器产出了估计（仍需检查 `status`/`uncertain`）；
`1` = 无法产出估计（如内点不足）；`2` = 请求非法。

生成示例请求并复现验收结果：

```bash
python3 examples/generate_examples.py   # 重新生成 examples/*.json（确定性）
python3 examples/run_demo.py            # 逐个跑 CLI 并对照真值打分
```

Python API：

```python
import numpy as np
from icp2d import estimate_pose, ICPConfig

result = estimate_pose(source, target, initial_pose=np.zeros(3), config=ICPConfig())
result.pose, result.status, result.residual_history, result.uncertain
```

## JSON 接口

请求（长度单位米，角度单位弧度）：

```json
{
  "source": [[x, y], ...],
  "target": [[x, y], ...],
  "initial_guess": [tx, ty, theta],   // 可选，默认单位变换
  "config": {"max_iterations": 50, "rejection_strategy": "mad", ...}  // 可选
}
```

响应（节选，`examples/03_degenerate_wall.json` 的真实输出）：

```json
{
  "success": true,
  "pose": {"x": -3.9e-05, "y": 0.29956, "theta": 1.5e-05},
  "status": "converged",
  "converged": true,
  "uncertain": true,
  "iterations": 4,
  "final_rmse": 0.00692,
  "residual_history": [0.29971, 0.00694, 0.00692, 0.00692],
  "num_inliers": 380,
  "num_source_points": 401,
  "covariance": [[1.27e-07, ...], ...],
  "hessian_eigenvalues": [11645.8, 380.0, 375.5],
  "degeneracy": {
    "degenerate": true,
    "flat_translation_direction": [1.0, 0.0],
    "rotation_flat": false,
    "translation_flatness_ratio": 7.3e-06,
    "message": "degenerate geometry: translation along (1.000, 0.000) is nearly unobservable; ..."
  },
  "iteration_log": [{"iteration": 1, "rmse": 0.2997, "num_inliers": 382, ...}, ...]
}
```

字段约定：

- `pose`：把 `source` 帧点变换到 `target` 帧的位姿，`q = R(theta) p + t`
- `status`：`converged`（步长与残差变化均低于阈值）/
  `max_iterations_reached` / `insufficient_inliers`
- `success`：仅当完全无法产出估计（内点不足）时为 `false`；
  退化但有解的问题仍返回 `true`，由 `uncertain`/`degeneracy` 标记
- `uncertain`：`degeneracy.degenerate` 或内点不足时为 `true`
- `covariance`：高斯-牛顿协方差 `σ²·H⁺`（3×3，顺序 tx, ty, theta；奇异时用伪逆）
- `residual_history`：每次迭代的内点 RMSE，长度等于迭代次数

`config` 可覆盖字段（`ICPConfig`）：`max_iterations`(50)、
`tolerance_translation`(1e-4)、`tolerance_rotation`(1e-5)、`tolerance_rmse`(1e-6)、
`max_correspondence_distance`(0.5)、`rejection_strategy`("mad")、
`trim_ratio`(0.8)、`mad_scale`(3.0)、`min_inliers`(6)、`min_iterations`(2)、
`degeneracy_flatness_ratio`(0.1)。未知字段会报错。

## 方法

### 最近邻关联

每次迭代把当前位姿变换后的 `source` 点对 `target` 做精确最近邻查询。
小规模用 NumPy 向量化暴力法，大规模自动切换 KD-tree（`icp2d/kdtree.py`，
测试中与暴力法逐点交叉验证）。

### 离群拒绝（显式、可组合）

`icp2d/outlier_rejection.py`，四种策略：

- `threshold`：距离超过 `max_correspondence_distance` 的关联直接丢弃
- `trimmed`：只保留距离最小的 `trim_ratio` 比例（抗部分重叠）
- `mad`（默认）：自适应门限 `median + mad_scale·1.4826·MAD`，并以
  `max_correspondence_distance` 为绝对上限；MAD 退化（距离全同）时回退到绝对门限
- `none`：不拒绝

除 `none` 外，所有策略都受绝对门限约束，灾难性错误关联不会进入求解器。

### 位姿估计与收敛

对残差 `r_i = R(θ)p_i + t − q_i` 做线性化最小二乘（高斯-牛顿），
每步解析求解 3×3 正规方程（奇异时回退 `lstsq`）。当平移步长、旋转步长
与残差变化同时低于阈值时报告 `converged`。注意：残差历史**不保证单调**——
内点集合随迭代变化（门限放入更多点后 RMSE 可能小幅回升），只保证总体下降。

### 退化检测（为什么不用 Hessian 特征值）

点到点 ICP 的高斯-牛顿 Hessian 几乎总是满秩的：每条关联同时约束变换后点的
两个坐标。因此经典的"走廊退化"（沿墙平移不可观测）**不会**表现为奇异
Hessian——实测直墙场景的 Hessian 特征值比仅 31，而解析协方差沿墙方向
std 仅 0.4 mm，实际误差却有 1.0 m（过度自信三个数量级）。

真实的退化体现在**代价景观**上：重新关联后，沿墙滑动源点云几乎不改变残差。
`icp2d/degeneracy.py` 在收敛位姿周围做经验探测：以固定 trim 比例（90%）的
对齐代价（不用自适应门限，保证代价函数良定义），沿 12 个方向扫描
±0.25/0.5/1.0 m 偏移的代价增量包络；最平坦方向与最刚硬方向的增量比低于
`degeneracy_flatness_ratio`（默认 0.1）即判定退化，并报告平坦方向。
旋转方向同理探测。实测分离度：直墙/走廊 0.002–0.07，矩形房间 0.56–0.75，
L 形 0.88。

## 验收场景与实测结果

`python3 examples/run_demo.py` 的真实输出（2026-09-25，Python 3.12.3 /
NumPy 2.5.3，Linux）：

```
example                            status                 uncertain     rmse   err_x   err_y  err_th
01_known_transform                 converged              False       0.0136  0.0004  0.0002  0.0000
02_outliers_partial_overlap        converged              False       0.0143  0.0003  0.0007  0.0000
03_degenerate_wall                 converged              True        0.0069  1.0000  0.0004  0.0000
04_local_optimum_square            converged              False       0.0070  0.0001  0.0001  1.5708
05_bad_initial_guess               insufficient_inliers   True             -       -       -       -
```

| 场景 | 验收点 | 结果 |
|---|---|---|
| 01 已知变换（噪声 σ=0.01） | 恢复 (0.8, −0.5, 0.15) | 误差 < 1 mm / 0.01° ✅ |
| 02 +180 离群点 +70% 重叠 | 离群拒绝后恢复真值 | 误差 < 1 mm，内点 329/505 ✅ |
| 03 直墙退化（真值 x 平移 1.0 m） | 返回不确定；可观测方向仍准 | `uncertain=true`，平坦方向 (1,0)；y/θ 误差 < 1 mm，x 误差 1.0 m 被如实标记 ✅ |
| 04 正方形房间 + π/2 错误初值 | 局部最优如实暴露 | 收敛且 RMSE 仅 0.007，但 θ 误差 90°——ICP 的已知局限，测试固化此行为 ✅ |
| 05 初值偏离 10 m | 不产生垃圾输出 | 门限拒绝全部关联，`insufficient_inliers`，退出码 1 ✅ |

自动化测试（49 项）覆盖：几何/KD-tree/拒绝策略单元测试、精确已知变换
（随机点云恢复到 1e-8）、噪声+离群+部分重叠、残差历史记录、收敛判据、
迭代上限、直墙退化（含低重叠含噪声变体）、对称局部最优、协方差半正定、
JSON 全流程、CLI 三种退出码。

## 已知局限

- **点到点度量的离散化偏差**：对无噪声的采样墙面，角点附近的跨边错误关联
  会形成近真值的力平衡，偏差量级为采样间距的一半（实测间距 0.1 m 时
  平移误差约 6–10 mm，间距减半偏差减半）。加性噪声会打破该对称性
  （场景 01/02 中误差 < 1 mm）。需要更高精度时应加密采样或改用点到线度量。
- **局部最优**：ICP 是局部方法，错误初值可能收敛到错误解（场景 04）。
  本库如实报告该结果（收敛状态 + 低残差可能具有欺骗性），不做多初值重启。
- **退化检测为经验探针**：阈值 0.1 在上述合成场景上分离良好，但对与探测
  尺度（≤1 m）相差悬殊的几何可能需要调整 `degeneracy_flatness_ratio`。
- 纯离线库：无实时性设计，点云规模数千点以内体验最佳。

## 实测运行记录

```bash
$ python3 --version && python3 -c "import numpy, pytest; print(numpy.__version__, pytest.__version__)"
Python 3.12.3
2.5.3 9.1.1

$ python3 -m pytest -q
.................................................                        [100%]
49 passed in 36.01s

$ python3 examples/generate_examples.py
wrote 01_known_transform.json (924 source, 924 target points)
wrote 02_outliers_partial_overlap.json (827 source, 924 target points)
wrote 03_degenerate_wall.json (401 source, 401 target points)
wrote 04_local_optimum_square.json (804 source, 804 target points)
wrote 05_bad_initial_guess.json (924 source, 924 target points)

$ python3 examples/run_demo.py
（输出见上表，全部符合预期）

$ python3 -m icp2d examples/01_known_transform.json --compact | head -c 200
{"success": true, "pose": {"x": 0.7996226270523367, "y": -0.5001797497787013, "theta": 0.1500326113516568}, ...}

$ python3 -m icp2d examples/05_bad_initial_guess.json --compact; echo "exit=$?"
{"success": false, "status": "insufficient_inliers", "uncertain": true, ...}
exit=1
```

未通过项：无（49/49 通过）。开发过程中发现并修复的问题：
退化探针初版使用自适应门限导致代价曲面病态（负曲率误判房间退化）、
离散化周期使小尺度探针测到伪刚度、`normalize_angle` 文档与实现区间不一致
——均已修复并由测试固化。

## 项目结构

```
icp2d/
  geometry.py          SE(2) 位姿运算（旋转、复合、求逆、角度归一化）
  kdtree.py            KD-tree + 向量化暴力法最近邻（自动调度）
  outlier_rejection.py 四种显式离群拒绝策略
  icp.py               点到点 ICP 主求解器（残差历史、收敛状态、协方差）
  degeneracy.py        代价景观经验探针（退化/不确定检测）
  synthetic.py         合成扫描生成（房间、直墙、噪声、离群、部分重叠）
  json_entry.py        JSON 请求校验与响应序列化
  cli.py / __main__.py 命令行入口（python3 -m icp2d）
tests/                 49 项 pytest 测试
examples/              5 个验收场景请求 + 真值 + 生成/打分脚本
```
