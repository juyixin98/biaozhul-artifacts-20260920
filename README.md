# 多机器人时空避碰（CBS 网格规划服务）

离散网格上的多机器人路径规划（MAPF）服务：使用 **CBS（Conflict-Based Search）**
求解多机器人时空避碰， FastAPI 提供 HTTP 接口， NumPy 表示栅格地图，
SciPy（`scipy.sparse.csgraph.dijkstra`）计算静态最短路（用作 A* 启发式与连通性预检）。
仅处理小规模离线实例，全部使用合成数据，不接真实硬件，不做可视化。

## 问题语义

- 4 邻接网格，动作 = 上下左右移动一格或原地等待，每步代价 1。
- **顶点冲突**：同一时刻两个机器人占据同一格 —— 禁止。
- **对向换边冲突（edge conflict）**：两个机器人在相邻两时刻互换格子 —— 禁止。
- **目标持续占格**：机器人到达目标后永远停留在目标格（仍参与冲突检测）。
- **优化目标**：最小化总代价 = 各机器人到达目标时刻之和（sum of costs）。
- 返回**可重放时间表**（每个机器人每时刻的 `(x, y, t)`）与**约束树统计**
  （高层节点数、扩展数、检测到的冲突数、低层调用次数）。

## 目录结构

```
app/
  grid.py        # 栅格地图 + SciPy 最短路（启发式/连通性）
  lowlevel.py    # 低层：带约束的时空 A*
  cbs.py         # 高层：CBS 约束树、冲突检测、解校验（重放）
  brute_force.py # 联合状态空间 Dijkstra 穷举（用于最优性核对）
  models.py      # Pydantic 请求/响应模型
  main.py        # FastAPI 应用（/health, /solve）
examples/
  instances.py     # 交叉口 / 单通道 / 无解 三个示例
  run_examples.py  # 直接调用求解器跑示例并与穷举结果比对
tests/
  test_cbs.py        # 交叉口、单通道、无解、目标占格、换边冲突等
  test_bruteforce.py # 随机小实例上 CBS 与穷举最优逐一比对
  test_api.py        # FastAPI TestClient 接口测试
requirements.txt  # 锁定依赖（pip freeze）
```

## 环境与启动

依赖：Python 3.12，其余全部锁定在 `requirements.txt`
（fastapi 0.141.1 / uvicorn 0.53.0 / numpy 2.5.3 / scipy 1.18.1 / pytest 9.1.1 / httpx 0.28.1 等）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 若 pypi.org 较慢，可用镜像：
# .venv/bin/pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

# 启动服务
.venv/bin/uvicorn app.main:app --port 8000

# 运行测试
.venv/bin/python -m pytest tests/ -q

# 运行示例（含与穷举最优的比对）
.venv/bin/python -m examples.run_examples
```

## API

### `GET /health`
返回 `{"status": "ok"}`。

### `POST /solve`

请求体：

```json
{
  "grid": {"width": 5, "height": 5, "obstacles": [[1, 0]]},
  "agents": [
    {"id": "east",  "start": [0, 2], "goal": [4, 2]},
    {"id": "south", "start": [2, 0], "goal": [2, 4]}
  ],
  "max_time": null
}
```

坐标为 `(x, y)`（x=列，y=行，原点左上）。`max_time` 可选，为单机器人搜索的时间上界。

响应（可解时）：

```json
{
  "status": "solved",
  "cost": 9,
  "makespan": 5,
  "timetable": {"east": [[0, 2, 0], [1, 2, 1], ...], "south": [...]},
  "stats": {"high_level_nodes": 3, "high_level_expanded": 2,
            "conflicts_detected": 1, "low_level_calls": 4},
  "errors": []
}
```

`timetable[agent_id]` 是 `[x, y, t]` 列表，t 从 0 逐步递增，可直接重放；
`errors` 为服务端对解做重放校验的结果（正常为空）。
不可解或输入几何非法时 `status` 为 `"unsolvable"`（非法输入在 `errors` 中说明）。

## 验收核对方式

`app/brute_force.py` 在**联合状态空间**（所有机器人位置的笛卡尔积）上跑 Dijkstra，
利用恒等式「总代价 = Σ_t（t 时刻前尚未到达目标的机器人数）」精确求最小总代价，
语义与 CBS 完全一致（同样的顶点/换边禁令、目标持续占格）。
`tests/test_bruteforce.py` 在 200+ 个随机/枚举小实例上逐一断言：
CBS 与穷举在「可解性」和「最优总代价」上完全一致，且每个解通过重放校验。

## 实测结果（2026-09-24，本机 Python 3.12.3）

- `pytest tests/ -q`：**16 passed**（约 50–70 秒，大部分时间花在穷举比对上）。
- `python -m examples.run_examples`：
  - 交叉口（5×5 空图，两机器人垂直穿越）：solved，cost=9，与穷举一致；
    约束树 3 节点 / 扩展 2 / 冲突 1。
  - 单通道（5×3，中间两列为 1 宽走廊，两端可错车）：solved，cost=11，与穷举一致；
    约束树 29 节点 / 扩展 15 / 冲突 14。
  - 严格 1×4 走廊对向互换：unsolvable，穷举亦确认无解。
- uvicorn 实机冒烟测试：`/health` 与 `/solve`（可解 + 无解）均正常。

## 限制与未完成项

- 仅面向**小规模离线实例**：穷举核对仅适用于微小地图（状态数随机器人数指数增长）；
  CBS 本身未做大规模基准测试，未实现 ICBS 等改进变体（如 disjoint splitting、
  冲突优先选择、对称推理），大规模实例可能较慢。
- `max_time` 默认取 `(机器人数+1) × 空格数`，对极小实例足够宽松，但不保证是所有
  实例的完备上界；超大等待需求的实例可能被误判为无解。
- 高层节点数硬上限 100000，超过即放弃并返回 unsolvable（防止失控）。
- 无持久化、无鉴权、无并发控制；服务为单进程内存求解。
- 低层 A* 的启发式为静态网格最短路（可采纳），未使用更安全但更贵的真间距启发式。
