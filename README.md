# 二维占据栅格概率更新后端（log-odds）

纯离线、合成数据的二维占据栅格地图后端。用 log-odds 累积射线观测，区分
**未知 / 空闲 / 占据** 三态，更新值有上下界（clamping）。不接任何真实硬件，
不做图形可视化，只输出终端文本。

## 功能

- log-odds 贝叶斯更新：空闲格加 `logit(p_free)`（负），回波终点格加
  `logit(p_occ)`（正）；每次更新后截断到 `[logit(p_clamp_min), logit(p_clamp_max)]`。
- 三态判定（严格比较）：`L > occ_threshold` 为占据，`L < free_threshold` 为空闲，
  其余为未知。默认阈值均为 0，即默认先验 0.5、一条射线即可定性。
- 射线遍历：Amanatides–Woo 栅格步进，Liang–Barsky 对地图矩形裁剪。
- 固定规则（见下节）：终点角点归属、越界裁剪、无回波、负坐标、角点穿越。
- 状态可导出为 JSON 并重新加载，加载时校验形状与取值范围。
- FastAPI HTTP 服务（内存注册表）：建图、射线更新、逐格查询、状态矩阵、
  导出 / 导入、删除、健康检查。

## 坐标与裁剪约定（固定）

- 世界坐标单位为米，连续值。单元 `(ix, iy)` 覆盖连续区域
  `[origin_x + ix*res, origin_x + (ix+1)*res) × [origin_y + iy*res, origin_y + (iy+1)*res)`。
- 世界坐标 → 单元索引：`ix = floor((x - origin_x)/res)`。**恰好落在格线上的点
  归索引更大的那个单元。**
- 射线先在连续单元坐标下用 Liang–Barsky 裁剪到地图矩形，再做遍历。
- 遍历为 super-cover 风格：射线**内部**恰好穿过格点角时，两个侧格和对角格都会
  被访问（顺序固定：先 y 侧格，再 x 侧格）；**终点恰好落在角点上时只贡献终点格**。
  每个格最多访问一次。
- 有回波：沿途所有格记空闲，终点格记占据；**终点在地图外时丢弃占据更新**，
  裁剪后的线段全部记空闲。
- 无回波（no return）：起点到 `max_range` 末端全部记空闲，不产生占据格。
- 零长度射线：有回波时仅把该格记占据，无空闲格。

默认传感器模型：`p_occ=0.7, p_free=0.3, p_clamp_max=0.99, p_clamp_min=0.01,
p_prior=0.5`（均为构造参数，可改）。注意 `logit(0.3) = -logit(0.7)`，因此
“一次空闲 + 一次占据”可精确抵消回先验。

## 目录结构

```
occupancy_grid/
  __init__.py     # 公共 API
  raycast.py      # 裁剪与射线遍历（纯函数，无状态）
  grid.py         # GridConfig / OccupancyGrid：log-odds 更新、查询、序列化
  app.py          # FastAPI 应用（内存注册表）
tests/
  test_grid.py    # 手工小网格逐格核对的单元测试
  test_api.py     # HTTP 端到端测试（TestClient）
examples/
  demo_offline.py # 合成数据离线演示脚本（终端文本）
```

## 依赖与安装

需要 Python 3.10+（开发环境为 3.12）。

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt          # 锁定版本，见 requirements.lock.txt
```

- 运行依赖：fastapi、uvicorn[standard]、numpy、scipy、pydantic（随 fastapi）。
- 测试依赖：pytest、httpx（TestClient 需要）。
- `requirements.lock.txt` 是本机 `pip freeze` 的完整锁定结果；`requirements.txt`
  是直接依赖及已验证版本。

## 运行

测试：

```bash
source .venv/bin/activate
pytest -q
```

离线示例（合成射线序列：重复射线、无回波、越界回波、负坐标、导出重载）：

```bash
python examples/demo_offline.py
```

启动 HTTP 服务：

```bash
uvicorn occupancy_grid.app:app --host 127.0.0.1 --port 8000
# 交互文档：http://127.0.0.1:8000/docs
```

### HTTP 接口速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/grids` | 建图，body 见下 |
| GET | `/grids` | 列出所有地图 |
| GET | `/grids/{id}` | 配置与三态计数 |
| POST | `/grids/{id}/rays` | 射线更新（有回波 / 无回波） |
| GET | `/grids/{id}/cells/{ix}/{iy}` | 单格 log-odds / 概率 / 状态 |
| GET | `/grids/{id}/states` | 整个状态矩阵 |
| GET | `/grids/{id}/export` | 导出完整状态 JSON |
| POST | `/grids/import` | 由导出 JSON 建图 |
| DELETE | `/grids/{id}` | 删除 |
| GET | `/health` | 健康检查 |

建图 body（全部字段除 nx/ny 外有默认值）：

```json
{"nx": 6, "ny": 5, "resolution": 1.0, "origin_x": -3.0, "origin_y": -2.0,
 "p_occ": 0.7, "p_free": 0.3, "p_clamp_max": 0.99, "p_clamp_min": 0.01,
 "p_prior": 0.5, "occ_threshold": 0.0, "free_threshold": 0.0}
```

有回波射线（给终点）：

```bash
curl -s -X POST http://127.0.0.1:8000/grids/$ID/rays \
  -H 'Content-Type: application/json' \
  -d '{"ox":-2.5,"oy":-0.5,"ex":1.5,"ey":-0.5}'
```

无回波射线（给角度与最大距离，方向用弧度）：

```bash
curl -s -X POST http://127.0.0.1:8000/grids/$ID/rays \
  -H 'Content-Type: application/json' \
  -d '{"ox":-2.5,"oy":-1.5,"angle":0.0,"max_range":3.0}'
```

## 测试覆盖

- 手工 5×4 / 6×5 小网格逐格核对：水平、垂直、非角点斜线、角点 super-cover、
  零长度射线。
- 重复射线：log-odds 单调累积并在截断界饱和；矛盾射线精确抵消。
- 无回波：只产生空闲格。
- 负坐标与非 1.0 分辨率：`floor` 映射与角点归属。
- 越界：起点或终点在图外的裁剪结果；终点越界丢弃占据更新。
- 边界点归属、非法参数拒绝、图外单元查询报错。
- 导出 → JSON → 重载：配置与 log-odds 矩阵完全一致；损坏数据被拒绝。
- HTTP 全接口：建图、更新、查询、越界/参数校验（422/400/404）、导出导入。

## 范围与限制

- 仅合成数据 / 离线回放；无传感器驱动、无实时性保证。
- 服务端地图只存内存，重启丢失；可用 export/import 持久化。
- 遍历与裁剪基于浮点连续坐标，角点规则按上述约定固定，浮点边界按
  `eps=1e-9` 容差处理。
- 未做多地图并发性能优化（注册表加锁保证正确性）。

## 实际运行记录（2026-09-24，Python 3.12.3 / Linux）

- 依赖安装：`pip install -r requirements.lock.txt` 成功（29 个锁定包，
  关键版本 fastapi 0.141.1、uvicorn 0.53.0、numpy 2.5.3、scipy 1.18.1、
  pytest 9.1.1、httpx 0.28.1）。
- 自动化测试：`pytest` —— **33 passed**。包括 2000 例固定种子随机射线，
  以独立的线段–AABB slab 求交暴力算法交叉核对 `traverse_cells` 的结果
  （覆盖各方向、轴对齐、越界、贴边）。
- 离线示例：`python examples/demo_offline.py` 正常退出；输出展示重复射线
  饱和（P(occ) 0.7→0.8448→…→0.9900 截断）、无回波仅记空闲、越界回波丢弃
  占据更新、负坐标角点、导出 908 字节 JSON 后重载状态完全一致。
- HTTP 冒烟：本地 uvicorn 实跑，health/建图/两类射线/单格查询/计数/
  export→import（states diff 为空）/422 参数校验均符合预期。
- 已知告警：Starlette 对 TestClient + httpx 有一条 deprecation warning
  （不影响功能；如未来版本移除可按其建议改用 httpx2）。

### 未完成 / 未做项

- 未接任何真实传感器或硬件回放文件格式（题目明确排除）。
- 未做图形可视化（题目明确排除）；示例只打印 ASCII 状态矩阵。
- 未实现批量射线更新接口和射线时序回放器（单条更新 + 循环调用即可，
  属后续可加项）。
- 未做服务端鉴权、持久化存储与多进程部署（内存型演示服务）。

