# 离散占据栅格融合(occupancy-grid-fusion)

纯后端 Python 离线计算库:将二维激光射线按 **log-odds 占据栅格**模型融合,
通过 JSON 文件作为输入/输出入口。只使用合成轨迹与传感器数据,不连接硬件,
不做可视化,不依赖 ROS。

## 功能

- 二维激光射线的 log-odds 占据更新:
  - **穿越单元**(射线经过但未终止的单元)累加 `l_free = log(p_free / (1 - p_free))`;
  - **终点单元**(仅当 `range < max_range`,即真实命中)累加 `l_occ = log(p_occ / (1 - p_occ))`;
  - 未命中射线(`range >= max_range`)只标记空闲,不产生占据证据。
- **概率截断**:log-odds 限制在 `[log(p_min/(1-p_min)), log(p_max/(1-p_max))]`
  (默认 `p_min=0.12, p_max=0.97`),防止数值无界增长。
- **传感器位姿**:每帧扫描携带 `(x, y, theta)`,射线终点经刚体变换到世界系。
- **越界处理**:终点或路径越界的射线,界内前缀正常更新,界外部分安全丢弃;
  障碍后方的单元不会被触碰(保持未知)。

## 项目结构

```
occupancy_grid/
  grid.py       # GridSpec / OccupancyGrid:log-odds 存储、坐标转换、截断
  geometry.py   # Bresenham 射线遍历、二维位姿变换
  fusion.py     # SensorModel / SensorPose / integrate_ray / integrate_scan
  io.py         # JSON 请求 -> 融合 -> JSON 结果
main.py         # 命令行入口
examples/request.json   # 请求样例(两帧八射线合成扫描)
tests/          # pytest 自动化测试(25 个用例)
```

## 安装与运行

依赖:Python 3.10+,NumPy,pytest(见 `requirements.txt`)。

```bash
pip install -r requirements.txt

# 执行融合(结果写入文件)
python3 main.py --request examples/request.json --output examples/result.json

# 或直接打印到标准输出
python3 main.py --request examples/request.json

# 运行测试
python3 -m pytest tests/ -v

# 覆盖率
python3 -m pytest tests/ --cov=occupancy_grid --cov=main --cov-report=term-missing
```

## 请求格式

```json
{
  "grid": {"width": 20, "height": 20, "resolution": 0.5,
           "origin_x": 0.0, "origin_y": 0.0, "p_min": 0.12, "p_max": 0.97},
  "sensor_model": {"p_occ": 0.7, "p_free": 0.4},
  "scans": [
    {"pose": {"x": 5.0, "y": 5.0, "theta": 0.0},
     "angles": [0.0, 1.5708], "ranges": [3.0, 8.0], "max_range": 8.0}
  ]
}
```

- `angles`:传感器系下的射线角(弧度);`ranges`:对应测距(米),两者等长。
- `range >= max_range` 视为未命中。
- 输出包含 `log_odds` 与 `probability` 两个二维数组(行 = y,列 = x)
  及每帧扫描的统计信息(`scan_stats`)。

## 手算验证示例(对应测试 `TestSingleRayHandCalc`)

栅格 10×10、分辨率 1.0、原点在 (0,0);传感器位姿 (0.5, 0.5, 0);
单条射线 `angle=0, range=3.0, max_range=8.0`:

- 终点世界坐标 (3.5, 0.5) → 单元 (0,3);
- 穿越单元 (0,0)、(0,1)、(0,2) 各加 `l_free = log(0.4/0.6) ≈ -0.4055`;
- 终点单元 (0,3) 加 `l_occ = log(0.7/0.3) ≈ 0.8473`;
- 其余单元(包括 (0,4)…(0,9),即障碍后方)保持 0(未知)。

## 实测记录(2026-09-25,本机 Python 3.12.3 / NumPy 2.5.3 / pytest 9.1.1)

| 命令 | 结果 |
|------|------|
| `python3 -m pytest tests/ -v` | **25 passed**,0 failed,0.78s |
| `python3 -m pytest tests/ --cov=occupancy_grid --cov=main --cov-report=term-missing` | 覆盖率 **100%**(193/193 行) |
| `python3 main.py --request examples/request.json --output examples/result.json` | 成功:2 帧扫描,12 条命中、4 条未命中;结果栅格中占据单元 12、空闲单元 16、未知单元 372 |

未通过项:无。

## 验收覆盖情况

- **单射线手算**:`tests/test_fusion.py::TestSingleRayHandCalc`(含旋转位姿);
- **重复观测**:`TestRepeatedObservations`(log-odds 线性累加 + 截断);
- **越界射线**:`TestOutOfBoundsRay`(终点越界、未命中出界、对角出界、传感器在界外报错);
- **障碍后方保护**:`TestObstacleShadow`(命中点后方的单元保持未知,未命中射线不产生占据)。
