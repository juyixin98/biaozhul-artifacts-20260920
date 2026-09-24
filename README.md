# 增量路径修复服务 (Incremental Path Repair)

二维加权栅格上的**增量**路径规划纯后端服务。算法为 **D\* Lite**（Koenig & Likhachev, 2002
规范实现），支持起点移动与局部代价（障碍 / 单元权重）增量更新；每次规划结果都与一次
**独立 Dijkstra** 全量搜索比较最优成本作为在线验收。Python + FastAPI + NumPy。

## 语义与规则

- **二维加权栅格**：每个单元有非负权重 `weight`（穿越代价因子）与独立的障碍标志 `blocked`。
  障碍不参与搜索；设为障碍期间权重保留，解除后恢复。
- **边代价**（对称）：直边 `(w_a + w_b) / 2`；斜边 `√2 · (w_a + w_b) / 2`。
- **负代价拒绝**：建图与每次更新在 Pydantic 层与服务层双重拒绝负权 / NaN / 无穷，
  变更先全量校验再原子落地（任一非法则整批不生效）。
- **障碍不可穿越**：端点为障碍则无边。
- **八邻接斜穿夹角禁止**：从 `(r,c)` 斜走到对角 `(r+1,c+1)` 时，若两个正交夹角单元
  `(r,c+1)`、`(r+1,c)` **都**是障碍，则该斜边禁止；只堵一侧时允许贴墙斜行。
  4 邻接模式下不存在斜边。
- **地图修改与规划快照绑定**：地图是一条只追加（append-only）的快照链。每个快照 id 由
  父快照 id + 规范化状态（形状、权重、障碍）+ 变更列表做 **SHA-256** 得到，可随时
  `/verify` 重算整条链校验，检测存储篡改。规划器始终绑定它当前地图视图的确切快照；
  更新必须携带 `expected_snapshot`，与链头不符返回 `409 snapshot_conflict`。
- **保留增量状态，但绝不沿用旧地图失效路径**：
  - 障碍 / 权重变化只重算受影响集合（变化单元及其几何邻居）的 `rhs`，再增量修复；
  - 起点移动按 D\* Lite 规范累计 `km` 偏移增量修复；
  - 当地图修改使自由单元最小权重**下降**、原启发式不再可纳时，显式**完整重置**搜索
    （响应诊断 `full_reset=true`），不静默沿用旧状态；
  - 规划端点（当前起点、目标）不允许被更新为障碍（`409 endpoint_blocked`，快照回滚）；
    需要改端点请用 `move-start` / 新建 / `reset`。
- **最优性验收**：每次响应同时返回 D\* Lite 结果与独立 Dijkstra 的最优成本
  （Dijkstra 不共享任何增量状态），`benchmark.cost_match=true` 才算通过；
  路径还会独立重算真实代价并校验。不可达时双方都必须为不可达，`path=null`。
- **诊断而非承诺**：响应中 `diagnostics.expanded_nonstale`（重扩展节点数）等是观测值，
  用于判断增量是否生效，**不是**任何形式的性能保证或硬阈值。

## 目录结构

```
app/
  grid.py       栅格、快照链、SHA-256 哈希绑定与校验
  planner.py    GridView(代价/邻接规则)、DStarLite、独立 Dijkstra
  service.py    内存注册表、快照绑定、最优性对拍
  schemas.py    Pydantic 请求模型与负权校验
  api.py        FastAPI 路由与错误处理
  main.py       应用入口 (app.main:app)
tests/          63 个 pytest 用例: 规则、快照、算法、随机对拍、HTTP 端到端
examples/       示例输入 + HTTP 演示脚本
requirements.txt / requirements.lock
```

## 本地启动

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt          # 或 pip install -r requirements.lock 锁定安装

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 交互式文档: http://127.0.0.1:8000/docs
```

## 验收命令

```bash
# 1) 自动化测试 (63 项: 斜穿规则/负权拒绝/障碍增删/不可达/起点移动/快照篡改/API/随机对拍)
.venv/bin/python -m pytest -q

# 2) 启动服务后运行端到端 HTTP 演示
uvicorn app.main:app --port 8000 &
.venv/bin/python examples/demo.py
```

## HTTP 协议摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/maps` | 建图：`rows, cols, connectivity(4|8), weights?, obstacles?` |
| `GET`  | `/api/maps/{id}` | 地图信息与全部快照版本 |
| `POST` | `/api/maps/{id}/updates` | 地图链追加版本：`changes[]`, `expected_snapshot?` |
| `GET`  | `/api/maps/{id}/verify` | 重算整条快照链 SHA-256 |
| `POST` | `/api/planners` | 基于地图（可指定历史快照）创建规划器：`map_id,start,goal` |
| `POST` | `/api/planners/{id}/plan` | 查询（必要时增量修复） |
| `POST` | `/api/planners/{id}/move-start` | 起点移动（增量） |
| `POST` | `/api/planners/{id}/update-map` | 在绑定地图上提交新版本并增量修复 |
| `POST` | `/api/planners/{id}/reset` | 放弃全部增量状态硬重置（可换快照 / 起点） |

变更项：`{"row", "col", "kind": "block"|"free"|"weight", "weight"?}`。

规划响应（节选）：

```json
{
  "ok": true,
  "reachable": true,
  "cost": 5.656854249492386,
  "path": [[0,0],[1,1],...],
  "path_cost": 5.656854249492386,
  "path_valid": true,
  "snapshot_id": "…sha256…",
  "snapshot_version": 1,
  "map_changed": true,
  "diagnostics": {
    "expanded_nonstale": 16,
    "heap_pops": 40,
    "stale_key_reinserts": 0,
    "rhs_recomputations": 67,
    "full_reset": false,
    "reason": "incremental_map_update"
  },
  "benchmark": {
    "dijkstra_cost": 5.656854249492386,
    "dijkstra_expansions": 25,
    "cost_match": true
  }
}
```

不可达时 `reachable=false`、`cost=null`、`path=null`，且 `benchmark.dijkstra_cost=null`。
若 D\* Lite 与 Dijkstra 成本不一致，响应 `ok=false, error_code=optimality_mismatch`
并如实给出两个成本（不会伪装成成功）。

## 快速 curl 走查

```bash
curl -s localhost:8000/api/maps -H 'content-type: application/json' \
  -d '{"rows":5,"cols":5,"connectivity":8}'
# 取返回 map_id 与 snapshot_id 后:
curl -s localhost:8000/api/planners -H 'content-type: application/json' \
  -d '{"map_id":"<MAP_ID>","start":{"row":0,"col":0},"goal":{"row":4,"col":4}}'
curl -s localhost:8000/api/planners/<PLANNER_ID>/plan -H 'content-type: application/json' -d '{}'
curl -s localhost:8000/api/planners/<PLANNER_ID>/update-map -H 'content-type: application/json' \
  -d '{"changes":[{"row":2,"col":2,"kind":"block"}]}'
curl -s localhost:8000/api/maps/<MAP_ID>/verify
```

## 正确性与测试说明

- 主循环采用带旧键重插（stale-key reinsert）的规范 `ComputeShortestPath`，优先队列用
  版本号惰性删除；键比较带 1e-10 容差，消除浮点累加导致的平局键序翻转。
- 除固定测试外，`tests/test_fuzz.py` 使用固定种子随机生成地图、障碍、权重（含零权高原、
  降地板触发重置）、障碍增删与起点移动交错序列，每步都与独立 Dijkstra 对拍成本与路径。
- 开发期间另跑过 1100 组更大规模随机对拍（脚本在开发会话中执行），成本与路径全部一致。
- 存储为进程内内存注册表，适合演示与验收；多副本持久化不在本交付范围。
