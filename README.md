# mapmatch — 二维地图轨迹匹配后端

基于 HMM / Viterbi 的二维道路图轨迹匹配（map matching）服务。**仅使用合成数据和离线回放，不连接任何真实硬件，不含可视化界面。**

## 算法说明

参考 Newson & Krumm (2009) 的隐马尔可夫模型框架：

- **候选生成**：对每个观测点，把所有道路边做折线投影，垂直距离 ≤ `candidate_radius` 的边作为候选状态。
- **观测（发射）概率**：`p(z|e) ∝ exp(-d²/(2σ²))`，`d` 为观测点到候选边的垂直距离。
- **转移概率**：用 `scipy.sparse.csgraph.dijkstra` 预计算路网全源最短路，得到相邻观测点两个候选投影点之间的**沿路行驶距离** `d_route`；`p ∝ exp(-|d_route − d_gps|/β)`，`d_gps` 为两点直线距离。**沿路不可达的候选对转移概率为 0**，因此匹配不可能在没有连通的边之间跳跃。
- **解码**：log 空间 Viterbi 求全局最优边序列。
- **置信差**：forward-backward 计算各候选的平滑后验概率，输出每点最优与次优候选的后验概率差（0–1），低于 `confidence_threshold` 标记 `low_confidence`。
- **时间断档**：相邻观测时间差 > `max_time_gap` 时把轨迹切成独立匹配片段，断档记录在 `time_gaps`。
- **不可达/无候选处理**：观测点无任何候选边 → `matched=false, reason=no_candidate`；某观测列候选从上游全部不可达 → 在该点重启 Viterbi；连续未匹配点汇总到 `unmatched_intervals`。

## 目录结构

```
mapmatch/
  graph.py      # 道路图、投影、候选生成、沿路最短路距离
  matcher.py    # HMM（Viterbi + forward-backward）、断档/不可达处理
  synthetic.py  # 合成场景：平行道路、立交无连接、时间断档、离群点
  schemas.py    # API 模型
main.py         # FastAPI 入口
tests/          # pytest 自动化测试（算法 + HTTP 接口）
examples/       # 离线示例回放
requirements.txt
```

## 依赖与安装

Python 3.12，依赖已锁定在 `requirements.txt`：

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## 启动服务

```bash
source .venv/bin/activate
uvicorn main:app --host 127.0.0.1 --port 8000
```

接口：

- `GET /health` → `{"status":"ok"}`
- `POST /match`：请求体

```json
{
  "graph": {
    "nodes": [{"id": "N0", "x": 0.0, "y": 0.0}],
    "edges": [{"id": "E0", "u": "N0", "v": "N1", "polyline": [[0,0],[100,0]]}]
  },
  "trajectory": [{"t": 0.0, "x": 5.0, "y": 1.0}],
  "params": {"sigma": 10.0, "beta": 8.0, "candidate_radius": 50.0, "max_time_gap": 10.0}
}
```

响应中每个匹配点包含 `edge_id`、`fraction`、投影坐标、`confidence_margin`、`segment_id` 等；另含 `unmatched_intervals`、`time_gaps`、`segments` 汇总字段。

## 运行测试

```bash
source .venv/bin/activate
python -m pytest -v
```

## 运行离线示例

```bash
source .venv/bin/activate
python examples/run_example.py
```

回放四个合成场景：① 平行道路（验证抗噪与道路区分）② 立交无连接（验证不在未连通边间跳跃）③ 时间断档（验证切段）④ 远离路网离群点（验证未匹配段输出）。

## 验收场景对应关系

| 验收要求 | 场景/测试 |
| --- | --- |
| 合成平行道路 + 噪声点 | `synthetic.make_parallel_*` / `test_parallel_roads_mostly_correct_road` |
| 立交无连接，不跨未连通边跳跃 | `synthetic.make_overpass_*` / `test_overpass_does_not_jump_between_unconnected_edges` |
| 不可达候选处理 | `test_overpass_graph_..._disconnected`、`test_far_outlier_is_unmatched_and_reported` |
| 时间断档 | `synthetic.make_gap_*` / `test_time_gap_splits_segments` |
| 置信差与未匹配段 | `test_confidence_and_low_confidence_flag`、响应字段 `confidence_margin` / `unmatched_intervals` |
| HTTP 后端 | `tests/test_api.py` |

## 实际运行结果（2026-09-24，Python 3.12.3）

- `python -m pytest -v`：**11 passed**（算法 8 项 + API 3 项），耗时约 2s。
- `python examples/run_example.py`：
  - 场景1 平行道路：91/91 点匹配，全部落在行驶的 A 路（0 次跳到平行的 B 路），逐边正确率 81/91（边界处因 x 向噪声被分到相邻边，属正常），置信差均值 0.901。
  - 场景2 立交无连接：91/91 点匹配，**0 次跳到几何相交但未连通的垂直路**，置信差均值 0.925。
  - 场景3 时间断档：正确切成 2 个片段，断档记录 `(index=10, dt=60s)`。
  - 场景4 离群点：中间远离路网的点标记 `matched=false, reason=no_candidate`，输出未匹配区间 `(2,2)`，其余点正常匹配。

## 已知限制 / 未完成项

- 全源最短路用 Dijkstra 预计算（`O(N²)` 内存），适合小规模合成路网；大规模路网应改为按需 A* 或收缩层次（CH）。
- 参数 `sigma`、`beta` 为固定默认值，未实现 Newson-Krumm 论文中的数据驱动估计。
- 道路均为二维直线段折线，未考虑转向限制、车道、速度模型。
- 无持久化、鉴权与可视化（按需求约定）。
