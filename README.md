# edt_field — 最近障碍距离场（二维栅格精确欧氏距离变换）

纯后端离线计算库：对二维占据栅格计算**精确欧氏距离变换**（Euclidean Distance
Transform, EDT），为每个栅格输出到最近障碍栅格的欧氏距离及该障碍的来源坐标。
只使用合成数据，不连接硬件、不依赖 ROS、不做可视化、不含前端。

## 特性

- **精确欧氏距离**：Felzenszwalb & Huttenlocher 抛物线包络两遍法，
  先行后列各做一次一维精确平方距离变换，总复杂度 O(rows × cols)。
  不是曼哈顿距离，也不是切角（chamfer）近似。
- **来源追踪**：每格同时输出最近障碍栅格的 `(行, 列)` 下标；障碍格来源为自身。
- **非方形格尺寸**：行、列方向分辨率 `dy`、`dx` 可不同，距离按各向异性加权。
- **退化地图**：全空地图距离为 `inf`（JSON 中为 `null`）、来源为 `-1`（JSON
  中为 `null`）；全障碍地图距离全为 0。
- **JSON 入口**：命令行读取请求 JSON、输出响应 JSON，便于离线批处理集成。

## 环境

- Python ≥ 3.10，NumPy（开发环境实测：Python 3.12.3 + numpy 2.5.3 + pytest 9.1.1）

```bash
pip install -r requirements.txt
```

## 库用法

```python
import numpy as np
from edt_field import euclidean_distance_transform

grid = np.zeros((4, 5), dtype=bool)
grid[1, 1] = grid[3, 3] = True          # 两个障碍
field = euclidean_distance_transform(grid, cell_size=(1.0, 1.0))  # (dy, dx)

field.distances    # (4,5) 每格最近障碍欧氏距离；全空地图为 inf
field.source_rows  # (4,5) 最近障碍行下标；全空地图为 -1
field.source_cols  # (4,5) 最近障碍列下标
```

## JSON 入口

```bash
python -m edt_field 请求.json            # 响应打印到标准输出
python -m edt_field 请求.json -o 响应.json
cat 请求.json | python -m edt_field -    # 从标准输入读取
```

请求格式（`cell_size` 可省略，默认 `[1.0, 1.0]`；也可写单个正数表示方形格）：

```json
{
  "grid": [[0, 0, 0], [0, 1, 0]],
  "cell_size": [0.5, 2.0]
}
```

响应格式：

```json
{
  "shape": [2, 3],
  "cell_size": [0.5, 2.0],
  "distances": [[...]],
  "nearest": [[[0, 1], ...]]
}
```

- `distances[r][c]`：该格到最近障碍的欧氏距离；全空地图为 `null`。
- `nearest[r][c]`：最近障碍的 `[行, 列]` 下标；全空地图为 `null`。
- 请求不合法（非矩形、取值非 0/1、格尺寸非正数等）时，错误信息输出到
  stderr，退出码为 2。

样例请求见 `examples/`：`request_small.json`（普通地图）、
`request_nonsquare.json`（非方形格）、`request_empty.json`（全空地图）。

## 测试

```bash
python -m pytest tests/ -v
```

验收策略：小图上用**暴力参考实现**（每格枚举全部障碍取最小欧氏距离）逐格
对照精确算法，并校验每个来源坐标确为障碍且其距离与报告值一致（并列最近点
时任一最近来源均可）。覆盖：随机地图（多种形状与格尺寸、障碍密度 0.0–1.0
扫描）、并列最近点、非方形格尺寸、地图边界（角部障碍到对角、仅边界障碍）、
全空/全障碍/单格地图、JSON 入口端到端与非法请求拒绝。

## 实际运行记录

以下为本项目开发环境中的真实运行结果（Linux, Python 3.12.3, numpy 2.5.3,
pytest 9.1.1）：

- `python3 -m pytest tests/ -v` → **89 passed in 0.68s**，无未通过项。
  （唯一一次失败发生在开发过程中：`request` 与 pytest 保留 fixture 同名导致
  收集错误，重命名参数后全部通过。）
- `python3 -m edt_field examples/request_small.json` → 正常输出 4×5 距离场，
  如格 (0,0) 距离 √2≈1.4142135623730951、来源 [1,1]，与手算一致。
- `python3 -m edt_field examples/request_nonsquare.json` → 非方形格
  `[0.5, 2.0]` 下格 (0,0) 距离 hypot(0.5, 4.0)≈4.031128874149275，来源 [1,2]。
- `python3 -m edt_field examples/request_empty.json` → 全空地图，
  `distances` 与 `nearest` 全部为 `null`。
- 性能抽查：500×500 随机地图（12262 个障碍格，格尺寸 0.05×0.1 m）计算耗时
  约 1.08s；随机抽 20 格与暴力参考逐一核对，距离全部一致（容差 1e-9）。

## 项目结构

```
edt_field/
  __init__.py     # 包入口，导出 euclidean_distance_transform / DistanceField
  core.py         # 精确欧氏距离变换核心（一维抛物线包络 + 两遍法 + 来源回溯）
  cli.py          # JSON 请求校验、响应生成、命令行入口
  __main__.py     # python -m edt_field 入口
tests/test_edt.py # 暴力参考对照与全部自动化测试
examples/         # 请求样例 JSON
requirements.txt
```
