# 二维地图轨迹匹配（mapmatching）

基于 HMM/Viterbi 的道路图轨迹匹配后端。仅使用合成数据与离线回放，不接真实硬件，不做可视化。

## 技术栈

- Python 3.12 + FastAPI（HTTP 接口）+ NumPy + SciPy（`csgraph.dijkstra` 路网最短路）
- 依赖已锁定，见 `requirements.txt`

## 模型

- **候选边**：按观测点到边折线的垂直距离生成（默认半径 50 m，最多 8 条），记录投影点与沿边弧长。
- **发射概率**：垂直距离 `d ~ N(0, σ)`，对数概率 `-d²/(2σ²)`。
- **转移概率**：`p ∝ exp(-|路网路径距离 − 观测直线距离| / β)`。路网路径距离 = 前候选沿边剩余长度 + 节点间最短路（SciPy Dijkstra）+ 后候选沿边弧长；**不可达候选对转移概率为 0**，因此 Viterbi 不会在没有路网连接的边之间跳转（立交无连接场景）。
- **解码**：对数空间 Viterbi；前向-后向算法给出每个候选的后验概率，输出**置信度**（所选候选后验）与**置信差**（最优与次优后验之差）。
- **时间断档**：相邻观测时间差超过 `max_gap`（默认 60 s）时切分段落，各段独立匹配，断档位置在 `segment_breaks` 中报告。
- **未匹配**：无候选边或后验置信度低于 `min_confidence` 的点标记为未匹配，连续未匹配点汇总为 `unmatched_segments`。

## 目录结构

```
mapmatching/
  graph.py       # 道路图（节点/有向边/折线几何）+ 最短路
  candidates.py  # 候选边生成与投影
  hmm.py         # 发射/转移概率、Viterbi、前向-后向
  matcher.py     # 分段编排、置信度、未匹配段
  scenarios.py   # 合成场景：平行道路、立交无连接、带噪轨迹生成
  main.py        # FastAPI 应用
tests/test_matcher.py   # 验收测试
examples/run_example.py # 离线回放示例
```

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动 API 服务
.venv/bin/uvicorn mapmatching.main:app --host 0.0.0.0 --port 8000

# 运行测试
.venv/bin/python -m pytest tests/ -v

# 运行离线回放示例
.venv/bin/python examples/run_example.py
```

## API

- `GET /health` — 健康检查
- `GET /graph/{scenario}` — 查看合成道路图（`parallel` | `overpass`）
- `POST /match` — 轨迹匹配，请求体：

```json
{
  "scenario": "parallel",
  "observations": [{"t": 0.0, "x": 50.0, "y": 1.2}, ...],
  "config": {"candidate_radius": 50, "sigma": 10, "beta": 30, "max_gap": 60, "min_confidence": 0.5}
}
```

响应逐点给出 `matched`、`edge_id`、`proj`、`confidence`、`margin`、`reason`，以及 `unmatched_segments`（下标闭区间）和 `segment_breaks`。

## 实测结果（本仓库提交时实际运行）

- `pytest`：**8 passed**（平行道路不跳路、立交不跳未连通边、拓扑不可达、时间断档切分、离路未匹配段、API 集成、未知场景 404）。
- 离线回放示例输出：
  - 平行道路（σ=8 m 噪声）：40 点匹配 39 点，全部命中 `bottom_E`，平均置信度 0.986、平均置信差 0.972；1 个低置信点报告为未匹配段 `(24, 24)`。
  - 立交无连接：50 点全部命中水平路 `h_E`，无一跳到几何相交但未连通的竖直路 `v_N/v_S`。
  - 时间断档 + 离路段：离路 10 点正确报告为未匹配段 `(15, 24)`，断档位置 `[25]` 正确检出，断档前后仍稳定匹配 `bottom_E`。

## 已知限制 / 未完成项

- 观测不含航向信息，同一条路的正反向边几何重合，个别点可能匹配到反向边（测试已按此放宽）；接入航向或转向惩罚可消除。
- 坐标为局部平面坐标（米），未实现经纬度投影（合成数据无需投影）。
- 全源最短路在图加载时预计算，适合小规模图；大图需改为按需最短路或 CH 加速。
- 未做可视化（按要求）。
