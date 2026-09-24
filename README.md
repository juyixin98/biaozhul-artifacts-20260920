# Hybrid A* 离线车辆路径规划服务

离散 **(x, y, θ) 方向状态** 的混合 A*（Hybrid A\*）搜索，使用自定义运动原语
（恒曲率圆弧 × 前进/倒车挡位），显式建模**最小转弯半径**与**倒车代价**，
并沿每条原语**连续密集采样做碰撞检查**。

- 纯离线 / 合成数据 / 无硬件连接 / 无图形可视化
- 技术栈：Python 3.12 · FastAPI · NumPy · SciPy（欧氏距离变换、Dijkstra 2D 启发式）

## 目录结构

```
hybrid_astar/
  grid_map.py     占据栅格 + SciPy EDT 预计算净空场（clearance）
  vehicle.py      车辆模型：长宽、最小转弯半径、车身圆覆盖足迹
  collision.py    沿原语采样的足迹圆碰撞检查
  primitives.py   恒曲率运动原语：闭式圆弧积分 + 密集采样
  heuristic.py    障碍感知 2D Dijkstra 启发式（8 连通，按车体膨胀）
  planner.py      Hybrid A* 搜索主体（状态离散化、代价、挡位切换惩罚）
  scenarios.py    三个合成验收场景
  validate.py     独立校验器（暴力碰撞复检 + 有限差分曲率检查）
  models.py       Pydantic API 模型
  app.py          FastAPI 应用（/health, /plan）
examples/
  run_examples.py 离线跑三个场景并打印结果（含零启发式基线对比）
tests/            22 个 pytest 自动化测试
requirements.txt  全部依赖精确锁定（含传递依赖）
```

## 环境与启动

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

直接依赖（均已在 requirements.txt 中精确锁定）：
`fastapi==0.141.1`、`uvicorn==0.53.0`、`numpy==2.5.3`、`scipy==1.18.1`、
`pydantic==2.13.5`、`pytest==9.1.1`、`httpx==0.28.1`。

启动 HTTP 服务：

```bash
uvicorn hybrid_astar.app:app --host 127.0.0.1 --port 8000
# GET  /health
# POST /plan
```

`POST /plan` 请求体示例：

```json
{
  "grid": [[0, 0, 1], [0, 0, 1]],
  "resolution": 0.5,
  "start": {"x": 1.0, "y": 1.0, "theta": 0.0},
  "goal":  {"x": 5.0, "y": 5.0, "theta": 1.5708},
  "vehicle": {"length": 2.0, "width": 1.0, "min_turning_radius": 2.0,
              "n_circles": 3, "margin": 0.1},
  "planner": {"use_heuristic": true, "allow_reverse": true,
              "reverse_penalty": 2.0, "goal_tol_xy": 0.75}
}
```

返回 `success / message / cost / expansions / elapsed_ms / path`，
每个路径点为 `{x, y, theta, gear}`（gear=+1 前进，-1 倒车）。

## 运行测试与示例

```bash
pytest                       # 全部 22 个测试
python -m examples.run_examples   # 三个验收场景 + 零启发式基线
```

## 方法要点

- **状态空间**：连续位姿，键值按栅格分辨率（x,y）与 `heading_bins=36`（10°）
  离散化；每个离散单元只保留最优 g 值节点。
- **运动原语**：曲率集 `{0, ±0.5κmax, ±κmax}` × `{前进, 倒车}`，共 10 条，
  弧长 1.0 m，闭式积分（直线/圆弧精确解），沿弧每 0.25 m 采样一次。
- **最小转弯半径**：`κmax = 1 / Rmin`（默认 Rmin=2 m），原语曲率不可能越界。
- **代价**：弧长 + 倒车倍数（×2）+ 挡位切换惩罚（2.0）+ 曲率小代价。
- **碰撞检查**：车身用 3 个沿中心线排列的圆覆盖；SciPy
  `distance_transform_edt` 预计算每格净空，任一个圆中心净空 < 圆半径即碰撞。
- **启发式**：从目标点出发、按车体半径膨胀障碍的 2D Dijkstra 距离场；
  忽略运动学但感知障碍，对路径长度下界可采纳（admissible）。因搜索在目标
  容差圆盘内即停止，启发式减去 `goal_tol_xy` 保证对"目标区域"可采纳；
  `use_heuristic=false` 时退化为零启发式 Dijkstra 基线。

## 验收结果（本机实际运行，Python 3.12.3, Linux x86_64）

`pytest`：**22 passed**（约 65 s，含无解场景的穷尽搜索）。

`python -m examples.run_examples` 实际输出摘要：

| 场景 | 结果 | cost | 扩展节点 | 耗时 |
|---|---|---|---|---|
| 窄通道（2 m 缺口）+ 启发式 | 成功，碰撞-free，\|κ\|≤限 | 12.075 | 82 | 0.12 s |
| 窄通道，**零启发式基线** | 成功，同价 12.075 | 12.075 | **6464** | 9.25 s |
| 倒车掉头（死胡同）仅前进 | **失败**（开列表耗尽） | — | 21 | — |
| 倒车掉头，允许倒车 | 成功，53 个采样点中 29 个为倒车 | 24.325 | 808 | 1.13 s |
| 无解地图（封闭箱体目标） | **失败**："no path found (open list exhausted)" | — | 14908 | 20.3 s |

- 运动学约束：对返回稠密路径做有限差分检查，最大观测曲率 0.5003（限 0.5）。
  超出的 0.0003 是**弦长采样的离散效应**（有限差分用的是相邻点弦长，而
  \|dθ\|/弦长 = κ·弧长/弦长 = κ(1+(κ·ds)²/24)，ds=0.25 时上界 +0.00033），
  校验器按原理设置 1e-3 容差，并非真实运动学违规；`κ=0.25` 的路径观测值
  恰为 0.2500，符合该公式。
- 碰撞约束：所有成功路径均由**独立的暴力复检**（每个足迹圆中心到每个障碍
  格的距离）确认无碰撞，不依赖规划器自身的距离变换。
- 零启发式基线：与启发式版本给出相同最优代价（12.075），但扩展节点数
  6464 vs 82（约 79 倍），验证启发式有效且不破坏最优性。
- HTTP 服务已用 uvicorn 实启并 `curl` / `/plan` 冒烟通过。

## 限制与未完成项

- 纯 Python 实现，大地图/高角度分辨率下较慢（无解 30×30 场景穷尽搜索约 20 s）；
  生产级需加 Reeds-Shepp/RS-shot 解析解扩展与 C++/向量化加速，未实现。
- 目标用容差圆盘+角度容差判定（默认 0.75 m / 20°），不保证精确落在目标点、
  目标朝向；原语晶格下精确到位需要更密的晶格或末端解析路径。
- 车辆为矩形→3 圆近似，未做完整多边形精确碰撞；`margin=0.1 m` 安全余量部分
  补偿该近似。
- 启发式忽略运动学，在需要大量倒车的场景中指导性弱（倒车场景 808 次扩展）。
- `starlette.testclient` 对 `httpx` 发出弃用警告（建议未来 httpx2），
  当前测试全部通过，未升级。
- 无可视化（按需求刻意不做）；路径以 JSON / 稠密采样点输出。
