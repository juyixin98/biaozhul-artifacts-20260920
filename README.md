# laser-deskew

离线二维激光扫描运动去畸变（deskew）服务。基于 Python + FastAPI + NumPy + SciPy，
只使用合成数据 / 离线回放，不连接真实硬件，不做可视化。

## 问题定义

旋转式二维激光雷达在一帧扫描中，每个点的采样时刻不同；机体同时在运动。
若把整帧当作同一时刻采到（刚性扫描假设），几何会被拖尾、扭曲。
本服务把每个点统一变换到**指定参考时刻的激光坐标系**中）。

## 坐标系与变换方向

- `world`：固定世界（地图）坐标系。
- `body`：机体坐标系，其运动由位姿序列 `T_world_body(t)` 给出（输入）。
- `laser`：激光雷达坐标系，通过**传感器外参** `T_body_laser = (x, y, theta)`
  刚性安装在机体上（laser 系在 body 系中的位姿）。

变换链（列向量约定，`A @ B` 表示先作用 `B`）：

```
p_laser_i = range_i * [cos(angle_i), sin(angle_i)]        # 激光系下测量点
p_world   = T_world_body(t_i) @ T_body_laser @ p_laser_i  # 采样时刻的世界坐标
p_ref     = T_body_laser^-1 @ T_world_body(t_ref)^-1 @ p_world
```

输出 `p_ref` 即**参考时刻 `t_ref` 的激光坐标系**下的点，等价于"雷达静止在
参考位姿时采到的扫描"。

位姿插值：x、y 线性插值，航向角 theta 在展开（unwrap）后线性插值
（匀速平移 + 匀速旋转模型）。**不做外推**：任何采样时刻或参考时刻超出位姿
序列覆盖区间时，请求被明确拒绝（HTTP 422）。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

依赖版本在 `requirements.txt` 中锁定。

## 启动服务

```bash
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
```

- `GET /health` → `{"status": "ok"}`
- `POST /deskew` → 去畸变

### POST /deskew 请求示例

```json
{
  "reference_time": 0.05,
  "extrinsic": {"x": 0.1, "y": 0.0, "theta": 0.0},
  "poses": [
    {"time": 0.0, "x": 0.0, "y": 0.0, "theta": 0.0},
    {"time": 0.1, "x": 0.05, "y": 0.01, "theta": 0.08}
  ],
  "points": [
    {"time": 0.0, "angle": -0.5, "range": 4.9},
    {"time": 0.05, "angle": 0.0, "range": 4.8}
  ]
}
```

响应：`points` 为参考时刻激光系下的 `(x, y)`；`frame` 字段固定为
`laser@reference_time`。位姿覆盖不足时返回 `422` 及原因说明。

## 运行测试

```bash
.venv/bin/python -m pytest tests/ -v
```

验收测试（`tests/test_deskew.py`、`tests/test_api.py`）：

1. 合成匀速平移 + 匀速旋转的机体扫描一面直墙（`app/synthetic.py`）；
2. 比较"未校正（刚性假设）"与"校正后"点到直墙的残差，校正后残差须显著下降
   （无噪声时降到数值精度量级）；
3. 位姿序列不覆盖部分扫描时刻、或参考时刻超出覆盖范围时，必须明确拒绝
   （核心库抛 `PoseCoverageError`，API 返回 422）。

## 运行示例（离线回放）

```bash
.venv/bin/python examples/run_example.py
```

通过 FastAPI TestClient 离线调用 `/deskew`，打印校正前后点到直墙残差，
以及缺少位姿覆盖时的拒绝行为。无需启动服务器。

## 项目结构

```
app/
  transforms.py     SE(2) 齐次变换（构造、求逆、作用）
  interpolation.py  位姿轨迹时间插值 + 覆盖检查（PoseCoverageError）
  deskew.py         去畸变核心：变换链实现
  synthetic.py      合成数据：匀速运动机体扫描直墙
  main.py           FastAPI 服务（/health, /deskew）
tests/              pytest 自动化测试
examples/run_example.py  离线回放示例
requirements.txt    锁定依赖
```

## 已知限制 / 未完成项

- 位姿插值为分段线性（匀速模型），未使用样条/李群插值；高速机动场景精度有限。
- 不做可视化（按要求）。
- 未接真实硬件与真实数据格式（如 rosbag、PCAP），仅支持 JSON 离线回放。
- 去畸变核心按点循环组合变换，点数极大时可再向量化优化。
