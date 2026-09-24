# 时空预约规划（Spatio-Temporal Reservation Planner）

小规模机器人（最多 **8** 台）在二维栅格地图上的**时空预约**纯后端服务。
每 tick 机器人只能 **wait（等待）** 或 **move（移动一格，4 邻域）**。
服务在预约时同时禁止：

1. **顶点冲突（vertex conflict）**：同一时刻两台机器人占据同一格；
2. **边冲突（edge conflict）——对向交换（swap）**：两台机器人在同一区间 `[t, t+1)`
   沿同一条无向边对向穿过。注意：对向交换在每个时刻的**顶点占用并不重合**，
   只检查顶点的实现会漏掉它，必须按 `(t, 边)` 单独检查；
3. **终点占用（endpoint hold）**：机器人到达终点后永久停留在该格，
   后续预约不得进入（含到达时刻）。

技术栈：Python 3.12 · FastAPI · SQLite（标准库 `sqlite3`，WAL 模式）。
无前端页面。

---

## 1. 快速开始

```bash
cd b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock.txt     # 锁定依赖；或 pip install -r requirements.txt

uvicorn app.main:app --host 127.0.0.1 --port 8000
# 数据库文件可用环境变量指定，默认 stp.db
STP_DB_PATH=/tmp/stp.db uvicorn app.main:app --reload
```

健康检查：

```bash
curl http://127.0.0.1:8000/health
# {"status":"ok","version":"1.0.0","maps":0}
```

交互式 API 文档（FastAPI 自带）：<http://127.0.0.1:8000/docs>。

### 验收命令

```bash
# A) 自动化单元 + API 测试（32 个，含真实多线程并发用例）
python -m pytest -q

# B) 端到端验收脚本：自动拉起一个真实 uvicorn 进程，用 HTTP 跑全部验收场景
python scripts/acceptance.py
# 也可以对已经运行的服务执行：
BASE_URL=http://127.0.0.1:8000 python scripts/acceptance.py
```

验收脚本覆盖：双向窄道、2 格最小对向交换（证明边冲突不会被漏掉）、
终点占用、凭证撤销后重规划、真实并发对向请求、版本陈旧与规划期间地图变化。

---

## 2. 协议

所有请求/响应均为 JSON。错误统一为：

```json
{ "error": { "code": "CONFLICT", "message": "...", "details": { ... } } }
```

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/api/maps` | 创建地图 |
| `GET` | `/api/maps/{map_id}` | 取地图（含 `map_version`、`reservation_version`、`content_hash`） |
| `PUT` | `/api/maps/{map_id}/obstacles` | 修改障碍（地图版本 +1，内容哈希重算） |
| `POST` | `/api/maps/{map_id}/reservations` | 规划并落库一条预约 |
| `POST` | `/api/maps/{map_id}/reservations/batch` | 多机器人按固定优先级联合规划，整批原子提交 |
| `GET` | `/api/maps/{map_id}/reservations` | 列出全部生效预约 |
| `POST` | `/api/maps/{map_id}/reservations/cancel` | 凭撤销凭证取消预约 |
| `GET` | `/api/maps/{map_id}/debug/state` | 调试视图（含 ASCII 终点图） |

### 2.1 创建地图

`examples/01_create_map.json`：

```bash
curl -s -X POST http://127.0.0.1:8000/api/maps \
  -H 'Content-Type: application/json' \
  -d @examples/01_create_map.json
```

响应：

```json
{
  "map_id": "demo", "width": 8, "height": 2,
  "obstacles": [[3, 0], [6, 1]],
  "map_version": 1,
  "content_hash": "sha256:…（64 位十六进制，对规范化地图内容取 SHA-256）",
  "reservation_version": 0,
  "created_at": "2026-…"
}
```

### 2.2 规划并预约（乐观版本控制）

请求（`examples/02_plan_robot1.json`）：

```json
{
  "robot_id": 1,
  "start": [0, 0],
  "goal": [7, 0],
  "horizon": 16,
  "expected_map_version": 1,
  "expected_reservation_version": 0
}
```

- `horizon`：规划窗口末端 tick（省略时按已有预约窗口与曼哈顿距离自动确定，上限 64）。
- `expected_map_version` / `expected_reservation_version`：**预期版本**。
  不一致立即返回 409（`MAP_VERSION_MISMATCH` / `RESERVATION_VERSION_STALE`），
  客户端需要重新拉取状态后再规划，服务不会替你在未知版本上落预约。
- 成功响应包含 `path`（格坐标序列，最后一格是终点）、`actions`
  （`tick`/`type=move|wait`/`from`/`to`）、`cancel_token`（**仅本次响应返回**）、
  `map_version`、`reservation_version`（本次提交后的值）、搜索统计与策略声明。

### 2.3 找不到路径时的冲突证据

返回 **409**，`details.evidence` 给出真实搜索证据，例如对向交换：

```json
{
  "error": {
    "code": "CONFLICT",
    "message": "no conflict-free path from (1, 0) to (0, 0) within horizon=1; closest blocking evidence: opposing swap on edge (1, 0)<->(0, 0) with robot 1 during [0,1]",
    "details": {
      "error_subtype": "NO_PATH_IN_HORIZON",
      "evidence": {
        "type": "NO_PATH_IN_HORIZON",
        "root_cause": "EDGE_CONFLICT_SWAP",
        "conflict_time": 0,
        "with_robot": 1,
        "from": [1, 0], "to": [0, 0],
        "horizon": 1,
        "nodes_explored": 2,
        "prune_counts": {"EDGE_CONFLICT_SWAP": 1}
      },
      "policy": {
        "priority": "fixed by robot_id ascending (1 highest)",
        "globally_complete": false,
        "reason": "fixed-priority sequential planning can block lower-priority robots even when a joint solution exists; cancel or adjust a higher-priority reservation and retry"
      }
    }
  }
}
```

证据类型：`VERTEX_CONFLICT`、`EDGE_CONFLICT_SWAP`、`ENDPOINT_OCCUPIED`、
`START_OCCUPIED`、`START_ON_OBSTACLE`、`GOAL_ON_OBSTACLE`、
`NO_PATH_IN_HORIZON`（带 `root_cause`）、`MAP_OBSTACLE_ON_PATH`、
`MAP_VERSION_MISMATCH`。

### 2.3 撤销预约

```bash
curl -s -X POST http://127.0.0.1:8000/api/maps/demo/reservations/cancel \
  -H 'Content-Type: application/json' \
  -d '{"robot_id": 1, "cancel_token": "<创建时返回的 64 位 hex>"}'
```

凭证错误返回 **403**（常量时间比较，见下）。撤销后 `reservation_version` +1，
此前占据的时空全部释放，可被重新预约。

---

## 3. 算法与语义

### 3.1 单机器人：时空 A\*

- 状态 `(cell, t)`，`g = t`，`h = 曼哈顿距离`；后继动作为 wait + 4 邻域移动。
- 每次展开按顺序检查：目标格 `t+1` 顶点占用 → 对方终点停留 →
  移动的对向交换边；命中即剪枝。
- 到达 `goal` 后，检查能否在 goal 上合法停留到窗口末端
  （其他机器人的终点停留是**永久**的，即使落在窗口外也会挡住终点）。
- 搜索失败时返回 A\* 推进过程中**离终点最近的一次真实剪枝**作为证据
  （平手取时刻更晚的），并附 `nodes_explored` 与各类剪枝计数，
  不是泛泛的 "no path"。
- 路径只记录到到达终点；到达之后的停留由 `reservation_endpoint`
  `(cell, arrival)` 表达，对后续规划而言在任意 `t >= arrival` 都成立。

### 3.2 固定优先级，且不保证全局完备

多机器人采用**固定优先级顺序规划**：按 `robot_id` 升序（1 最高），
先到先得的已预约路径是硬约束，低优先级机器人必须为高优先级让路。

**这不保证全局完备**：高优先级机器人的某个选择（例如直接堵死唯一窄道）
可能使低优先级机器人无解，即使存在一个所有机器人都能通行的联合解
（该解需要高优先级机器人让路/等待/绕行）。此时服务如实返回带
`policy.globally_complete=false` 的冲突证据，由调用方撤销或调整高优先级预约后重试。
这是显式的工程取舍：可预测、单调、无需回溯搜索，适合小规模、低延迟场景，
代价是放弃最优性/完备性保证。

`/reservations/batch` 在单次请求内按同样固定优先级顺序搜索，
批内把更高优先级机器人的候选路径作为约束，整批原子提交。

### 3.3 乐观并发与"规划期间地图变化"

规划是耗时计算，不允许长时间持锁。流程：

1. **短临界区取快照**：地图、`map_version`、`reservation_version`、全部生效预约；
2. 校验调用方给的两个 expected 版本，陈旧立即 409；
3. **锁外跑时空 A\***；
4. 进入写事务后**重新读取版本并用最新预约对整条路径重新校验**（`revalidate_path`）：
   - 地图版本变了 → `MAP_VERSION_MISMATCH`（路径未在新地图上校验过，绝不静默落库）；
   - 与并发落库的预约冲突 → 回滚，用最新快照重规划（最多 3 次尝试），
     仍冲突则返回真实冲突证据；
   - 通过 → 原子替换旧预约、`reservation_version+1`、写审计日志。

数据库层还有一道纵深防御：`reservation_edge` 上对
`(map_id, t, 无向边端点)` 建唯一索引——对向交换双方规范到同一条无向边，
即使应用层漏判，`INSERT` 也会被数据库拒绝。

### 3.4 密码学操作（全部真实执行）

位于 `app/crypto.py`，仅用标准库：

- 预约 ID / 撤销凭证：`secrets.token_hex`（CSPRNG）。
- 落库只保存凭证的 **SHA-256**（`hashlib.sha256`），明文凭证只在创建响应里出现一次。
- 撤销校验：`hmac.compare_digest` 做**常量时间**比较（防时序侧信道），错误返回 403。
- 地图内容哈希：对 `sort_keys` 的规范化 JSON 取 **SHA-256**，`GET /maps` 返回，
  客户端可复算比对；障碍每次变更都重算并令 `map_version` +1。

---

## 4. 典型场景演示

```bash
# 5x1 双向窄道
curl -s -X POST localhost:8000/api/maps -H 'Content-Type: application/json' \
  -d '{"map_id":"c","width":5,"height":1}' >/dev/null
curl -s -X POST localhost:8000/api/maps/c/reservations -H 'Content-Type: application/json' \
  -d '{"robot_id":1,"start":[0,0],"goal":[4,0]}'
# 200，路径 [0,0]->[1,0]->[2,0]->[3,0]->[4,0]
curl -i -X POST localhost:8000/api/maps/c/reservations -H 'Content-Type: application/json' \
  -d '{"robot_id":2,"start":[4,0],"goal":[0,0]}'
# HTTP/1.1 409，evidence 指向 robot 1（窄道里无处避让）
```

最小对向交换（2 格，顶点检查会漏掉）：

```bash
curl -s -X POST localhost:8000/api/maps -H 'Content-Type: application/json' \
  -d '{"map_id":"sw","width":2,"height":1}' >/dev/null
curl -s -X POST localhost:8000/api/maps/sw/reservations -H 'Content-Type: application/json' \
  -d '{"robot_id":1,"start":[0,0],"goal":[1,0],"horizon":1}' >/dev/null
curl -i -X POST localhost:8000/api/maps/sw/reservations -H 'Content-Type: application/json' \
  -d '{"robot_id":2,"start":[1,0],"goal":[0,0],"horizon":1}'
# 409，root_cause = EDGE_CONFLICT_SWAP
```

---

## 5. 目录结构

```
b/
├── app/
│   ├── main.py          # FastAPI 路由与统一错误处理
│   ├── schemas.py        # Pydantic 协议模型
│   ├── service.py        # 快照/锁外搜索/提交前重校验/乐观重试/批处理
│   ├── planner.py        # 时空 A*：顶点+对向交换+终点占用；revalidate_path
│   ├── database.py       # SQLite 表结构、唯一约束、原子事务
│   ├── crypto.py         # secrets / SHA-256 / hmac.compare_digest
│   └── errors.py         # 结构化错误码
├── tests/
│   ├── test_planner.py   # 算法单测（边冲突是重点）
│   └── test_api.py       # API + 真实多线程并发 + 地图中途变化
├── scripts/acceptance.py  # 拉起真实 HTTP 服务的端到端验收脚本
├── examples/             # 示例请求 JSON
├── requirements.txt
├── requirements.lock.txt
└── pytest.ini
```

## 6. 已知边界 / 设计取舍

- 单机单实例：并发安全由进程内锁 + SQLite 事务 + 唯一索引保证；
  不支持多实例水平扩展（如需，应把串行点下沉到单连接写库或加分布式锁）。
- 时间原点固定为 `t=0`，所有预约共享同一离散时钟；最大 horizon 64，最多 8 台机器人。
- 固定优先级顺序规划**不保证全局完备也不保证总路径最短**；这是明确取舍而非缺陷。
- 同一机器人在同一地图上只保留一条生效预约，重复规划即原子替换旧预约。
