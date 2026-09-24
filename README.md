# 增量路径修复服务（D* Lite · FastAPI · NumPy）

二维加权栅格的**增量路径规划**纯后端服务。核心算法为 **D\* Lite**
（Koenig & Likhachev, AAMO 2002：固定目标、起点可移动、边代价局部更新的标准增量算法），
每次规划结果都与一次**独立的、无状态的 Dijkstra** 重新计算结果对比最优成本，
并以 SHA-256 地图/规划快照 + HMAC-SHA256 响应签名保证"地图修改与规划快照绑定"。

> 输出的 `reexpanded_nodes`（重扩展节点数）只是**诊断指标**，反映增量修复实际重新处理了多少节点，
> **不是**任何形式的硬性能承诺（"一定比 Dijkstra 快 N 倍"之类）。

## 功能与语义

- **加权栅格**：`cost[y][x]` 表示*进入*该格所付代价；起点格免费（路径成本=沿途进入格代价之和）。
- **拒绝负代价**：建图与更新时所有代价必须为有限非负数，否则 `422`；零代价合法（启发式自动降为平凡 0）。
- **障碍不可穿越**：障碍格不能作为任何边的端点。
- **邻接**：可选 4 邻接（正交）或 8 邻接（正交+对角）。
- **斜穿规则**（`diagonal_rule="two_blocked"`）：对角移动 `(x,y)→(x+dx,y+dy)`
  仅当两个正交夹角格 `(x+dx,y)` 与 `(x,y+dy)` **同时**为障碍（或出界）时禁止——
  即禁止斜穿两障碍夹角；只有一侧是障碍时允许贴墙滑行。D\* Lite 与 Dijkstra 使用同一规则。
- **起点移动**：可移动任意距离（`km` 按旧起点到新起点的启发式距离累加，标准 D\* Lite 第 IV 节做法）。
- **局部代价更新**：稀疏地修改任意格的代价/障碍标志；增量状态被**修复**而非盲目沿用。
- **快照绑定**：
  - 快照载荷覆盖完整地图状态 + 起终点，以规范化 JSON 做 **SHA-256** 摘要得到 `snapshot_id`；
  - 每个写/规划请求必须携带 `expected_snapshot_id`，客户端基于过期快照操作会收到
    **409 `snapshot_mismatch`**，绝不会把旧地图下已失效的路径当作当前结果返回；
  - 增量 g/rhs 状态只在当前快照下有效；代价缩放下界降低时会用新键重建优先队列，保证正确性。
- **密码操作真实执行**：`hmac`/`hashlib`/`secrets` 实际运算，提供 `/verify` 端点校验签名；
  篡改报文体或用其他密钥伪造签名都会校验失败（测试覆盖）。
- **不可达**：目标不可达时 `reachable=false`、`cost=null`、`path=null`；此时最优性判据为
  独立 Dijkstra 同样报告不可达。

## 目录结构

```
app/
  planner.py   # Grid、DStarLite、独立 Dijkstra（基线 + 最优成本核对）
  crypto.py    # 规范化快照载荷、SHA-256 摘要、HMAC-SHA256 签名/验签（真实实现）
  models.py    # Pydantic 请求/响应模型（含负代价等校验）
  store.py     # 线程安全会话存储：地图修订、快照、增量状态绑定
  main.py      # FastAPI 路由
tests/         # 71 个 pytest 用例（算法 + HTTP + 密码学）
examples/
  create_map.json      # 示例建图输入
  cost_updates.json    # 示例局部更新输入
  demo.py              # 标准库实现的端到端验收脚本
requirements.txt       # 直接依赖（固定版本）
requirements-lock.txt  # 完整传递依赖锁定
```

## 本地启动

```bash
cd /home/admin/Downloads/biaozhul/P062/a

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-lock.txt   # 安装锁定依赖

uvicorn app.main:app --host 127.0.0.1 --port 8000
```

交互式 API 文档：http://127.0.0.1:8000/docs （OpenAPI/Swagger，真实由服务生成）。

## 验收命令

```bash
# 1) 自动化测试（算法、HTTP、快照、签名全覆盖；无需先启动服务）
python -m pytest tests/ -q

# 2) 端到端演示（需先按上面命令启动服务；另开一个终端）
python examples/demo.py
# 末尾应打印：ALL ACCEPTANCE STEPS PASSED
```

### 手动 curl 流程

```bash
# 建图（返回 map_id、snapshot_id、hmac_key）
curl -s localhost:8000/maps -H 'Content-Type: application/json' \
  --data @examples/create_map.json | tee /tmp/map.json

MID=$(python3 -c "import json;print(json.load(open('/tmp/map.json'))['map_id'])")
SNAP=$(python3 -c "import json;print(json.load(open('/tmp/map.json'))['snapshot_id'])")

# 规划（响应同时给出 dstar 的 cost 和独立 dijkstra_cost 及 optimal_match）
curl -s localhost:8000/maps/$MID/plan -H 'Content-Type: application/json' \
  -d "{\"map_id\":\"$MID\",\"expected_snapshot_id\":\"$SNAP\"}"

# 局部代价/障碍更新（auto_replan 默认 true，直接返回修复后的规划）
curl -s localhost:8000/maps/$MID/costs -H 'Content-Type: application/json' \
  -d "{\"map_id\":\"$MID\",\"expected_snapshot_id\":\"$SNAP\",\"updates\":$(cat examples/cost_updates.json)}"
```

## HTTP 协议

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/maps` | 建图，201 返回 `map_id` / `snapshot_id` / 一次性 `hmac_key` |
| `GET` | `/maps/{id}` | 查看会话状态（修订号、快照、起终点） |
| `POST` | `/maps/{id}/plan` | 在指定快照上规划 |
| `POST` | `/maps/{id}/costs` | 稀疏更新单元格（`cost` 数字 / `blocked` 布尔；`cost=null` 表示阻塞） |
| `POST` | `/maps/{id}/move-start` | 移动起点 |
| `POST` | `/maps/{id}/verify` | 校验规划响应的 HMAC-SHA256 签名 |
| `DELETE` | `/maps/{id}` | 删除会话 |
| `GET` | `/health` | 健康检查 |

**错误**：`404` 未知地图；`409` 快照不匹配（`error=snapshot_mismatch`）；
`422` 负代价/NaN/阻塞起点/越界/阻塞目标等。

**规划响应关键字段**：

```jsonc
{
  "snapshot_id": "...",          // 本次结果绑定的快照
  "reachable": true,
  "cost": 6.0,                   // D* Lite 结果
  "path": [[0,4], ...],          // 贪心沿 g 值提取的路径
  "path_cost_check": 6.0,        // 服务端沿路径在当前地图上重走累加的真实代价
  "path_valid": true,            // 起点/终点正确、边可通行、无环、成本吻合
  "dijkstra_cost": 6.0,          // 独立 Dijkstra 最优成本
  "optimal_match": true,         // 两者一致（不可达时要求两者都不可达）
  "reexpanded_nodes": 3,         // 诊断：本次真正弹出并处理的节点数
  "dijkstra_settled_nodes": 41,  // 基线 Dijkstra 定居节点数
  "vertex_updates": 18,
  "km": 2.0,
  "signature": "..."             // HMAC-SHA256(snapshot_id + "." + 规范化响应体)
}
```

签名核验：取响应 JSON 去掉 `signature` 字段，按 `sort_keys=True, separators=(",", ":")`
规范化为 UTF-8，计算 `HMAC_SHA256(key, snapshot_id + "." + body)`（hex）。
建图时返回的 `hmac_key` 即密钥（每图 32 随机字节，仅在建图响应中出现一次）。

## 正确性依据与测试场景

- **算法**：`app/planner.py` 中 D\* Lite 完整实现 rhs/g 值、优先队列陈旧项识别、
  超一致/欠一致分支、`km` 累加、边变更影响集 `{c}∪neighbors(c)` 更新；
  启发式为按全图最小进入代价缩放的 Chebyshev（8 邻接）/曼哈顿（4 邻接）距离，可采纳且一致。
- **基线**：`dijkstra_full` 每次新建独立堆重算，不共享任何增量状态。
- **测试**（`tests/`，共 71 项）覆盖需求点名的全部场景：
  - 障碍新增导致不可达、障碍移除后修复；
  - 不可达目标（D\* Lite 与 Dijkstra 必须一致）；
  - 起点近距/远距/随机移动；
  - 斜穿两障碍夹角禁止、贴墙滑行允许、4 邻接无对角；
  - 负代价 / NaN / inf 拒绝，零代价地图；
  - 40 组随机 fuzz（4/8 邻接、混合增删障碍、改代价、移起点，逐快照对拍 Dijkstra）；
  - 每次返回路径在**当前**地图上逐边走一遍，杜绝旧地图失效路径残留；
  - 过期快照 409、相同地图摘要相同、HMAC 签名往返校验/篡改/伪造失败。

## 设计取舍说明

- 目标在建图时固定（D\* Lite 的算法前提）；换目标需新建地图。
- 会话状态存于进程内存（单进程、线程安全）；多副本部署需替换为共享存储，
  但快照校验机制本身不依赖存储位置。
- 当更新引入比历史最小值更小的单元代价时，启发式缩放下界改变，会以 O(N) 重建队列键
（g/rhs 不变）；仅"代价降低类"编辑触发，是保证正确性的保守回退。
