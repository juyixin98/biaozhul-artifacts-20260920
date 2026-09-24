# 时空预约规划（Spatio-Temporal Reservation Planner）

小规模机器人（最多 **8** 个）在二维栅格上的**时空预约**纯后端服务：
Python + FastAPI + SQLite。机器人每个离散时刻只能**等待**或**移动一格**；
规划同时禁止两类冲突，且**不能只检查顶点冲突而漏掉边冲突**：

1. **顶点冲突（vertex/node conflict）**：同一刻两个机器人占同一格；
2. **边冲突 / 对向交换（edge/swap conflict）**：同一对相邻时刻两个机器人交换位置
   （A：u→v，B：v→u）。只看每刻"谁在哪个格"发现不了这种冲突，必须检查有向边。

机器人到达终点后**无限等待**，因此其终点格自到达刻起对其他机器人**永久占用**。

---

## 1. 目录结构

```
app/
  planner.py     空间-时间 A*（时空状态 (x,y,t)），顶点+边+永久终点约束，失败证据
  crypto.py      HMAC-SHA256 撤销令牌（真实签名 + 常量时间比较）
  db.py          SQLite 模式/WAL/默认场景（地图、R1..R8、密钥、版本）
  service.py     乐观并发控制：快照规划 -> 提交前重新比对版本并复核路径
  main.py        FastAPI 路由 / Pydantic 协议 / 统一错误信封
tests/
  test_planner.py     算法层（边冲突反证、证据、固定优先级非完备、会让湾）
  test_api.py         HTTP 层（窄道、终点占用、撤销、版本、地图期间变化、批量原子）
  test_concurrent.py  独立 uvicorn 子进程 + 8 并发请求验收
examples/             示例请求 JSON
scripts/acceptance.sh curl 端到端验收脚本
requirements.txt      直接依赖
requirements.lock     完整锁定依赖（pip freeze，可复现安装）
```

---

## 2. 本地启动

需要 Python 3.10+（开发验证用 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P061/a

# 方式 A：用锁定文件创建虚拟环境（推荐，可复现）
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock

# 方式 B：系统已具备依赖时可直接用
# pip install -r requirements.txt

# 启动（数据库默认在 ./spatio.db，可用环境变量覆盖）
SPATIO_DB=./spatio.db .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8000
```

启动后：

- 服务首页 / OpenAPI：<http://127.0.0.1:8000/docs>
- 健康检查：`GET /health`

---

## 3. 验收命令

### 3.1 自动化测试（28 个用例）

```bash
.venv/bin/python -m pytest -q
```

其中 `tests/test_concurrent.py` 会在**独立子进程**中拉起真实 uvicorn，用多线程
同时发起 **8 个并发规划请求**，断言：没有 5xx、同一永久终点只有 1 个赢家、
数据库中的预约逐刻模拟**既无顶点冲突也无边冲突**；并验证输家拿到 `412` 后用
新版本重试可以成功。

### 3.2 curl 端到端验收脚本

先启动服务（见上），再另开一个终端：

```bash
bash scripts/acceptance.sh
```

脚本依次验收：双向窄道边冲突（证据里必须出现 `type=edge`）、终点永久占用、
乐观版本 412、**篡改撤销令牌 403**、正确撤销后终点腾出可重新规划、批量失败
原子性（一个都不落库）、会让湾地图批量成功。结尾打印
`ALL ACCEPTANCE CHECKS PASSED ✔`。

### 3.3 手工试一条（窄道对向交换）

```bash
curl -sS -X POST 127.0.0.1:8000/admin/reset >/dev/null
curl -sS -X PUT  127.0.0.1:8000/map -H 'Content-Type: application/json' \
  -d '{"width":4,"height":1,"obstacles":[]}'
# ...放置机器人见 scripts/acceptance.sh 第 1 步
curl -sS -X POST 127.0.0.1:8000/reservations/plan -H 'Content-Type: application/json' \
  -d @examples/01_r2_cross_corridor.json
curl -sS -X POST 127.0.0.1:8000/reservations/plan -H 'Content-Type: application/json' \
  -d @examples/02_r1_faces_edge_conflict.json   # 409，证据含 edge
```

---

## 4. HTTP 协议

所有错误统一信封：`{"error": {"code", "message", "details"}}`。

| 方法 & 路径 | 说明 |
| --- | --- |
| `GET  /health` | 健康检查 |
| `GET  /state` | 地图（含版本）、预约版本、8 个机器人状态、全部活跃预约路径 |
| `POST /admin/reset` | 恢复默认 8×5 会让湾地图与 R1..R8 |
| `PUT  /map` | 替换地图（宽/高/障碍），内容变化时地图版本 +1；返回受影响的既有预约 |
| `PUT  /robots/{id}` | 注册/移动机器人（最多 8 个，起点必须可通行且不与他人重合） |
| `POST /reservations/plan` | 单个机器人规划并提交 |
| `POST /reservations/plan-batch` | 固定优先级批量规划，**原子提交** |
| `POST /reservations/{resv_id}/cancel` | 凭 HMAC 令牌撤销 |

### 4.1 规划请求字段

```jsonc
// POST /reservations/plan
{
  "robot_id": "R1",
  "goal": {"x": 3, "y": 0},
  "expected_map_version": 3,            // 可选，乐观锁
  "expected_reservation_version": 1,    // 可选，乐观锁
  "horizon": 200,                       // 可选，时间窗上限
  "max_nodes": 200000                   // 可选，扩展节点上限
}
```

成功响应（节选）：

```json
{
  "resv_id": "ab12...",
  "robot_id": "R1",
  "path": [[0,0],[1,0],...],            // path[k] 是刻 k 占据的格子
  "actions": [{"time":0,"type":"move","from":[0,0],"to":[1,0]}, ...],
  "start_time": 0,
  "arrival_time": 3,
  "goal": [3,0],
  "map_version": 3,
  "reservation_version": 2,
  "cancel_token": "<base64url(payload)>.<base64url(HMAC-SHA256)>"
}
```

`actions[].type` 为 `wait`（相邻格相同）或 `move`（4-邻接一格）。

### 4.2 找不到路径：409 + 冲突证据

```jsonc
{
  "error": {
    "code": "NO_FEASIBLE_PATH",
    "message": "...贪婪、非完备规划器，失败不等于全局无解",
    "details": {
      "robot_id": "R1",
      "start": [0,0],
      "goal": [3,0],
      "evidence": {
        "kind": "exhausted",            // start_blocked | permanent_goal | exhausted
        "detail": "在时间窗 200 内不存在 ...",
        "blockers": [
          {"type":"vertex","cell":[1,0],"time":1,"blocker_ref":"reservation:../R2"},
          {"type":"edge","src":[1,0],"dst":[2,0],"time":1,"blocker_ref":"reservation:../R2"}
        ]
      }
    }
  }
}
```

`blockers[].type` 取值 `vertex` / `edge` / `permanent`。窄道对向场景下证据中
**必然包含 `edge`**，`tests/test_planner.py::test_naive_vertex_only_planner_misses_swap`
专门反证：关掉边检查的"天真"规划器会输出一条顶点表完全干净、却在对向交换的路径，
独立复核器 `validate_path` 必须把它判为 `edge` 冲突。

批量失败（`/reservations/plan-batch`）额外返回 `priority_order`、`failed_robot`
与 `failure`，并且**整个批次不写入任何预约**。

### 4.3 版本与错误码

| HTTP | code | 含义 |
| --- | --- | --- |
| 412 | `MAP_STALE` / `RESV_STALE` | 请求携带的预期版本与当前快照不符 |
| 412 | `MAP_CHANGED_DURING_PLANNING` | 规划期间地图被改，提交时复核失败 |
| 412 | `RESV_CHANGED_DURING_PLANNING` | 规划期间预约表被并发事务改动 |
| 412 | `REVALIDATION_CONFLICT` | 提交前在最新状态上复核路径发现冲突 |
| 409 | `NO_FEASIBLE_PATH` | 固定优先级下找不到无冲突路径（含证据） |
| 409 | `ROBOT_BUSY` / `RESV_NOT_ACTIVE` / `COMMIT_CONFLICT` | 忙碌/重复撤销/数据库层竞争 |
| 403 | `INVALID_TOKEN` / `TOKEN_RESERVATION_MISMATCH` | 撤销令牌伪造或张冠李戴 |
| 404 | `ROBOT_NOT_FOUND` / `RESV_NOT_FOUND` | 资源不存在 |

---

## 5. 关键设计

### 5.1 空间-时间 A*

状态是 `(x, y, t)`，`g = t`，启发式为到目标的曼哈顿距离（等待不改变位置，
故 h 可采纳）。扩展动作是等待与 4 邻接。每个候选动作依次检查：目标格在 `t+1`
的顶点占用、永久终点占用、`t→t+1` 的无向边占用（同刻反向穿越即交换）。

### 5.2 固定优先级，明确**不保证全局完备**

`/reservations/plan-batch` 按机器人 **id 升序的固定顺序**逐个规划，先规划者的
路径立刻变成后者的硬约束，且**不会回溯、不会让高优先级者重规划**。这是多智能体
路径规划中经典的 *prioritized planning*：实现简单、效率高，但是**贪婪、非完备**
——即使一组目标存在联合可行解，早期机器人的自私选择也可能把后来者堵死。
本服务因此：

- 成功结果是经过逐刻独立复核的真实无冲突路径；
- 失败结果只声明"在该优先级与当前预约下找不到"，并返回把搜索堵死的
  顶点/边/永久终点证据，**不**宣称"全局无解"。

单个规划接口把所有已有活跃预约视为更高优先级约束，语义一致。

### 5.3 乐观并发控制（地图版本 + 预约版本）

- `maps.version`：每次**内容确实变化**的地图更新 +1。
- `reservation_version`：全局整数，每次成功提交或撤销预约 +1（批量按条数增加）。

请求可携带两个"预期版本"。处理流程：

1. 读一致性快照并先与预期版本比对（不符 → 412，要求重读）；
2. 在快照上跑 CPU 密集搜索（事务外，不持写锁）；
3. 提交时 `BEGIN IMMEDIATE` 拿写锁，**重新读取并比对两个版本**，并在最新地图 +
   最新预约上用 `validate_path` 对路径做顶点/边/永久终点二次复核；
4. 任一不匹配 → 412 作废，请客户端基于新版本重试；全部通过才写入。
   `vertex_res (time,x,y)` 主键在数据库层兜底，杜绝并发写同刻同格。

这覆盖了"**规划期间地图变化须重新校验**"：搜索期间另一个请求改了地图，提交时
地图版本必然不同，返回 `MAP_CHANGED_DURING_PLANNING`（测试用确定性钩子与真实
并发两种方式分别验收）。

### 5.4 撤销预订（真实密码学）

每条预约返回一个 cancellation token：

```
base64url(JSON{resv_id, robot_id, nonce}) . base64url(HMAC_SHA256(base, server_secret))
```

- 服务首次启动用 `secrets.token_hex(32)` 生成密钥并持久化于 SQLite，重启后仍有效；
- 撤销时**重新计算 HMAC** 并以 `hmac.compare_digest` 常量时间比较，再校验
  payload 里的 `resv_id` 与路径参数一致；伪造、篡改、张冠李戴都返回 403；
- 撤销删除该预约的时空占用行并把预约行标记为 `canceled`（保留审计），
  预约版本 +1，机器人终点随即腾出供他人规划；重复撤销返回 409。

### 5.5 默认场景

默认地图为 8×5，中间 `y=2` 是东西向单行窄道，`(2,1)`、`(5,3)` 是两个会让湾，
R1..R4 在北侧、R5..R8 在南侧。相向交通可以借港湾错车——
`test_bay_allows_opposing_traffic_to_pass` 与验收脚本第 10 步对此做了逐刻
独立模拟验证（既无同格也无交换）。

---

## 6. 说明与限制

- 时间原点固定为 `t=0`；当前版本面向一次性任务编排，不支持给单机器人增量改约，
  需改约时先撤销再规划（撤销后终点腾出）。
- 地图更新不会自动删除既有预约，响应中的 `affected_reservations` 报告哪些预约
  在新地图上经过了不可通行格，交由调用方决定是否撤销。
- 所有规划、签名、版本比较均为真实计算；测试中的失败均按真实 HTTP 状态码与
  证据如实断言。
