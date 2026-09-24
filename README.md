# 多机器人时空避碰（CBS）

离散网格上的多机器人路径规划服务。基于 **CBS（Conflict-Based Search）** 求
**总代价最优**（各机器人到达目标时刻之和最小）的无冲突时刻表，仅处理小规模离线实例，
全部使用合成数据，不接真实硬件，不做可视化。

## 问题语义

- 网格地图：`.` 可通行，`#` 障碍；每时刻机器人可四邻移动一格或原地等待；
- **顶点冲突**：两机器人同一时刻占据同一格 —— 禁止；
- **边冲突（对向换边）**：两机器人在同一时间段内沿同一条边对向交换位置 —— 禁止；
- **目标持续占格**：机器人到达目标后永久停留该格，仍参与后续冲突判定；
- **代价**：各机器人首次到达目标的时刻之和（sum of individual costs）；
- 无解判定：先在联合状态空间做 BFS 可行性预检（语义与 CBS 一致），不可行
  直接判 `unsolvable`；可解时 CBS 低层搜索设有完备时间上限 `F^n · 2^n`
  （F 自由格数、n 机器人数，即联合状态数上界），保证搜索必然终止。

## 目录结构

```
mrcbs/
  grid.py        # 网格解析、最短路距离表（NumPy + SciPy dijkstra）
  lowlevel.py    # 带时空约束的单机器人 A*
  cbs.py         # CBS 高层：冲突检测、约束树、统计
  brute_force.py # 联合状态空间一致代价搜索（穷举核对最优性）
  validate.py    # 时刻表校验器（可重放验证）
  main.py        # FastAPI 服务
tests/           # pytest 自动化测试
examples/run_examples.py  # 离线示例回放
requirements.txt # 锁定依赖
```

## 依赖与启动

需要 Python 3.12（开发环境为 3.12.3）。

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动服务（默认 8000 端口）
.venv/bin/uvicorn mrcbs.main:app --host 127.0.0.1 --port 8000

# 运行测试
.venv/bin/python -m pytest tests/ -v

# 运行离线示例
.venv/bin/python examples/run_examples.py
```

## API

### `POST /solve`

```json
{
  "grid": ["...", "...", "..."],
  "agents": [{"start": [1, 0], "goal": [1, 2]}, {"start": [0, 1], "goal": [2, 1]}],
  "max_nodes": 10000,
  "check_brute_force": true
}
```

返回（节选）：

- `status`: `optimal` / `unsolvable` / `aborted` / `invalid`
- `cost`: 最小总代价；`makespan`: 时刻表跨度
- `timetable`: 可重放时刻表，每机器人一个等长格子序列（到达后重复目标格）
- `stats`: 约束树统计（`nodes_expanded`、`nodes_generated`、`max_open_size`、
  `conflicts_detected`、`constraints_in_solution`、`time_bound`）
- `brute_force_cost` / `optimal_verified`: 穷举核对结果（`check_brute_force=true` 时）

### `POST /validate`

提交 `grid`、`agents` 与 `timetable`，返回 `valid` 与逐条违规描述
（顶点冲突、边冲突、非法移动、进入障碍、到达后离开目标等）。

### `GET /health`

健康检查。

## 验收覆盖

- **穷举核对**：`brute_force.py` 在联合状态空间做一致代价搜索，测试中对
  交叉口、让行港湾及 20 个随机小实例逐一比对 CBS 与穷举的总代价；
- **交叉口**：3x3 两机器人垂直穿越，最优总代价 5（一方等待一步）；
- **单通道**：1x3 对向互换判定无解；带让行港湾的通道可解且代价与穷举一致；
- **无解情况**：目标格被先到者永久占用、单通道对向均判定无解，穷举复核一致；
- **可重放时刻表**：`/solve` 返回等长时刻表，`/validate` 可离线重放校验。

## 实测结果（2026-09-24，Python 3.12.3）

- `pytest tests/`：**39 passed**（约 0.8s），含 20 个随机小实例的 CBS↔穷举代价一致性；
- `examples/run_examples.py`：
  - 交叉口：最优总代价 5，穷举核对一致，时刻表校验通过
    （约束树：扩展 2 节点、检测 1 次冲突）；
  - 带让行港湾通道：最优总代价 7，穷举核对一致（扩展 10 节点、9 次冲突）；
  - 单通道对向互换：判定无解，穷举复核同样无解；
  - 目标格被先到者永久占用：判定无解，穷举复核同样无解；
- 实机启动 `uvicorn mrcbs.main:app` 后用 curl 验证：`/health`、`/solve`
  （含 `check_brute_force` 返回 `optimal_verified: true`）、`/validate`
  （合法时刻表通过、注入顶点冲突被检出）均正常。

## 已知限制 / 未完成项

- 仅面向小规模离线实例：穷举核对与可行性预检的联合状态空间随
  （自由格数 × 机器人数）指数增长，实测适用范围约 2~3 个机器人、十余自由格；
  更大实例 CBS 本身可用，但会跳过穷举核对（预检超限时按可解继续，
  约束树超 `max_nodes` 返回 `aborted` 而非确切无解）。
- 代价模型为总到达时刻之和（sum of individual costs），未实现 makespan 最优。
- 未做可视化、未接真实硬件（按要求）；无增量式/在线重规划。
- 测试运行有一条 Starlette 弃用警告（`httpx` TestClient 提示），不影响结果。
