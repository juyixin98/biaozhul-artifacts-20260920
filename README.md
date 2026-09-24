# 二维占据栅格概率更新（Occupancy Grid Backend）

纯后端实现：用 log-odds 累积射线观测，区分未知 / 空闲 / 占据三态。
**只用合成数据 / 离线回放，不连接任何真实硬件，也不做可视化。**

技术栈：Python 3.12、NumPy（状态存储与计算）、SciPy（`scipy.special.expit`
做 log-odds→概率转换）、FastAPI（HTTP 服务）、pytest（自动化测试）。

## 目录结构

```
occupancy_grid/
  grid.py      # OccupancyGrid 核心：坐标变换、射线/扫描更新、三态分类
  raycast.py   # Bresenham 射线栅格化 + Liang–Barsky 线段裁剪
  io.py        # 状态导出/加载（.npz，含格式版本号）
  api.py       # FastAPI 服务（内存栅格注册表）
examples/
  run_example.py   # 离线合成回放示例（无界面）
tests/
  test_grid.py     # 核心算法手工核对测试
  test_api.py      # HTTP 接口测试
requirements.txt   # 锁定版本依赖
```

## 依赖与安装

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

锁定版本（详见 `requirements.txt`）：

| 包 | 版本 |
|---|---|
| numpy | 2.1.3 |
| scipy | 1.14.1 |
| fastapi | 0.115.5 |
| uvicorn | 0.32.1 |
| pydantic | 2.9.2 |
| pytest | 8.3.4 |
| httpx | 0.27.2（测试用 TestClient） |

## 运行测试

```bash
pytest -v
```

## 运行离线示例

```bash
python examples/run_example.py
```

示例用 3 组合成扫描（含重复射线与无回波）累积一张 8×8 的小地图，
打印字符化状态、墙面格概率，并做一次导出/再加载一致性校验。
该字符打印是命令行文本输出，不是可视化界面。

## 启动 HTTP 服务

```bash
uvicorn occupancy_grid.api:app --host 127.0.0.1 --port 8000
```

交互式文档：`http://127.0.0.1:8000/docs`。

### 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/grids` | 创建栅格（宽高、分辨率、原点、log-odds 参数） |
| GET | `/grids` | 列出所有栅格 |
| GET | `/grids/{id}` | 栅格信息 |
| POST | `/grids/{id}/rays` | 集成若干条手工射线（传感器点、终点、是否命中） |
| POST | `/grids/{id}/scan` | 集成一帧扫描（位姿、相对角度、距离、最大量程） |
| GET | `/grids/{id}/cell?ix=&iy=` | 单格 log-odds 与状态 |
| GET | `/grids/{id}/log_odds` | 整张 log-odds 数组 |
| GET | `/grids/{id}/probabilities` | 整张占据概率数组 |
| POST | `/grids/{id}/save` | 导出 `.npz` |
| POST | `/grids/load` | 从 `.npz` 加载为新栅格 |

最小调用示例：

```bash
curl -s -X POST localhost:8000/grids -H 'content-type: application/json' \
  -d '{"width":5,"height":5,"resolution":1.0,"l_occ":1.0,"l_free":-0.5}'
# 用返回的 grid_id：
curl -s -X POST localhost:8000/grids/<id>/scan -H 'content-type: application/json' \
  -d '{"pose_x":0.5,"pose_y":0.5,"angles":[0.0],"ranges":[2.0],"max_range":5.0}'
curl -s "localhost:8000/grids/<id>/cell?ix=2&iy=0"
```

## 算法约定（固定规则）

- **坐标系**：世界平面（米，可含负坐标）。栅格覆盖矩形
  `[ox, ox+w·r) × [oy, oy+h·r)`，`(ox,oy)` 是格 (0,0) 的最小角世界坐标，
  可以为负。格号 `ix = floor((x-ox)/r)`；正好落在外边界上的点钳入最后一格。
- **射线更新**：传感器原点到测量终点的线段先用 Liang–Barsky 裁剪到地图矩形；
  完全在图外的射线不更新。
  - 命中（`range < max_range`）且终点在图内：路径格加 `l_free`，终点格加 `l_occ`。
  - 命中但终点在图外：只对裁剪后线段经过的格加 `l_free`，不产生占据证据
    （地图无法存储界外信息）。
  - 无回波（`range >= max_range`）：射线截到 `max_range`，全部加 `l_free`。
  - 每条射线更新后，log-odds 钳制在 `[l_min, l_max]`（默认 ±4）。
- **三态分类**：`l>0` 占据、`l<0` 空闲、`l==0` 未知；概率 `p=sigmoid(l)`，
  未知格恰为 0.5。
- 栅格化用 Bresenham，每条射线上每格只更新一次；重复射线按次累积。
- 默认传感器模型：`l_occ=ln(0.7/0.3)≈0.847`、`l_free=ln(0.4/0.6)≈−0.405`。
- **导出格式**：`.npz` 内包含全部构造参数、原始 float64 log-odds 数组及
  `format_version`；再加载逐位一致，格式版本不符会报错。

## 测试覆盖（验收点对照）

- `test_single_hit_ray_hand_computed`：5×5 手工小网格逐格核对 free/occ/未知；
- `test_repeated_rays_accumulate` / `test_log_odds_clamping`：重复射线累积与上下界；
- `test_miss_marks_only_free`：无回波只标空闲；
- `test_negative_world_coordinates`：负原点、负坐标射线；
- `test_hit_endpoint_outside_map_is_free_only` / `test_ray_fully_outside_map_is_ignored`：
  终点出界与完全出界的裁剪规则；
- `test_save_load_roundtrip_via_api` / `test_save_load_roundtrip`：导出再加载保持状态；
- 另有扫描旋转、概率值、坏版本号拒绝、HTTP 错误路径等用例。

## 实测结果（2026-09-24，Python 3.12.3）

- `pytest -v`：**23 passed, 1 warning in 0.99s**（唯一 warning 来自
  starlette TestClient 的 anyio 弃用别名，与本项目代码无关）。
- `python examples/run_example.py`：3 组合成扫描共 21 次格子更新；
  墙面格 log-odds=1.695（恰为 2×ln(0.7/0.3)，两次命中累积），p_occ=0.845；
  导出再加载 log-odds 逐位一致（`identical: True`）。
- uvicorn 冒烟测试：建图 → 扫描 → 查格（OCCUPIED, log-odds=1.0）→
  导出 → 再加载 → 查格结果一致，全部符合预期。
- 环境备注：默认 PyPI 源在本机下载超时，依赖实际经清华镜像
  `pypi.tuna.tsinghua.edu.cn` 安装；版本与 `requirements.txt` 锁定值一致。

## 未完成项 / 已知限制

- 栅格注册表在内存中，服务重启即丢失（持久化靠显式 save/load）。
- 射线更新为逐格 Python 循环，未做向量化；大地图高频扫描下性能未优化。
- 无并发写锁：同一 grid_id 的并发更新请求未做串行化。
- 未实现地图膨胀（inflation）、衰减（decay）与多机器人地图融合。
