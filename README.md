# 电量可达任务分配后端（Battery-Reachable Dispatch）

纯后端服务：为多台合成机器人分配运输任务。能耗由**距离、载荷、等待时长**的
显式公式计算；只有在「完成任务后能抵达**任一可用充电点**且保留安全余量」的
前提下才允许接单；多机器人采用可解释的**最小成本匹配**；分配与电量/充电点
预占在**同一个数据库事务**内提交，重复接单由数据库唯一索引强制拒绝。

> ⚠️ 本系统只生成**合成（simulated）控制指令**（响应内明确标注
> `simulated: true` / `do_not_send_to_real_robot: true`），不连接、不向
> 任何真实机器人或控制器下发命令。

---

## 1. 能耗模型（真实计算，`app/energy.py`）

平面坐标，距离单位米，电量单位 Wh（合成模型）。常量均可通过环境变量覆盖。

| 量 | 公式 | 默认常量 |
|---|---|---|
| 距离 | `d(a,b) = hypot(Δx, Δy)` | — |
| 行驶能耗 | `E_move(d, kg) = (α + β·kg) · d` | `α=0.18 Wh/m`, `β=0.0006 Wh/(m·kg)` |
| 等待能耗 | `E_wait(s) = γ · s` | `γ=0.012 Wh/s` |
| 任务总能耗 | `E_move(d_out, payload) + E_wait(wait_s) + E_move(d_ret, 0)` | 返航按**空载**（载荷已在任务点卸下） |

**可达性（硬性安全条件）**

```
当前可用电量 − 任务总能耗 ≥ 安全余量（默认 20 Wh）
```

「当前可用电量」= 机器人最新实测电量（未来若引入多任务排队，再减去既有活跃
分配的 `reserved_energy_wh`；当前每台机器人同一时刻只允许一条活跃分配）。

---

## 2. 匹配算法（可解释）

`POST /api/dispatch` 在**一个 `BEGIN IMMEDIATE` 事务**内完成：

1. 枚举每个 (任务 × 空闲机器人) 候选边，为每条边选择返航能耗最小的可用且
   未被占用的充电点，并按上面的公式与余量判定 `feasible`；
2. 在所有可行边中按 `(总能耗, 任务id, 机器人id)` 升序依次取边——
   **总是先取全局最便宜的可行配对**；任务、机器人、充电点任一被占用则跳过；
3. 若边的首选充电点在本批次内被更便宜的边先占用，则就**剩余充电点重新计算**
   （该边返回的 `note` 会说明 "claimed earlier … recomputed"）；
4. 对最终选中的边写入 `allocations`：预占电量 `total + margin`、预占充电点、
   任务置 `assigned`、机器人置 `busy`，随事务一起提交；
5. 响应返回**全部候选边**及完整能耗拆解（`candidate_edges`），所以每条
   接受/拒绝决定都可被人工复核；拒绝原因例如
   `insufficient_battery_after_safety_margin`（**局部最便宜但无法返航**）、
   `no_available_charger`、`all_chargers_claimed`。

算法名在响应中固定返回：`greedy-minimum-cost-feasible-matching/v1`。

### 防重复 / 竞争（SQLite 部分唯一索引，数据库层强制）

- 每个任务最多一条 `planned/running` 分配（不能重复接任务）；
- 每台机器人最多一条活跃分配；
- 每个可用充电点最多被一条活跃分配预占（竞争不会双重预占）。

---

## 3. 充电点失效

`POST /api/chargers/{id}/fail`：

- **未开始（planned）** 的受影响任务：取消其分配、释放电量与充电点预占、
  任务回到 `pending`，写 `reservation_released_recompute` 告警，并**立即用
  当前可用充电点集合自动重算**；没有替代时任务保持 `pending` 并如实报告
  `no_available_charger`。
- **执行中（running）** 的任务：**保留不动**（不能安全地静默取消），
  写 `running_mission_charger_failed_risk`（**critical**）风险告警，
  携带基于公式的预测剩余电量，提示需改航。

---

## 4. 遥测：实测电量 vs 预测

`POST /api/robots/{id}/telemetry` 可带 `mission_completed_distance_m`
（自任务开始实际走完的路程）。服务按路径分段（满载去程 / 空载返航）计算
此刻预测剩余电量；当实测值显著低于预测（超过 1% 或 0.5 Wh 容差）时写
`battery_lower_than_predicted`（**critical**）告警，含 deficit 与余量数据。

---

## 5. API（除 `/healthz` 外全部需要 HMAC 签名）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查（免签） |
| POST | `/api/robots` | 注册机器人 |
| POST | `/api/chargers` | 注册充电点 |
| POST | `/api/tasks` | 创建任务 |
| POST | `/api/dispatch` | 最小成本匹配 + 事务预占 |
| POST | `/api/tasks/{id}/start` | 任务开始（记录起始电量快照） |
| POST | `/api/tasks/{id}/complete` | 完成（可用 `measured_battery_wh` 结算） |
| POST | `/api/tasks/{id}/cancel` | 取消**未开始**任务的预占 |
| POST | `/api/robots/{id}/telemetry` | 上报实测电量/位置 |
| POST | `/api/chargers/{id}/fail` | 充电点失效（自动重算+风险告警） |
| GET | `/api/allocations` | 全部分配记录 |
| GET | `/api/alerts?severity=critical` | 告警列表 |
| GET | `/api/snapshot` | 当前全局状态快照 |

请求/响应示例见 `examples/`。交互式文档：服务启动后访问 `/docs`。

### HMAC-SHA256 签名协议（真实密码操作，标准库 `hmac`/`hashlib`）

请求头：

```
X-Key-Id:     demo-key-1
X-Timestamp:  <unix 秒>          # 与服务器时差需 ≤ 300s
X-Nonce:      <随机串>           # 一次性，SQLite 去重防重放
X-Signature:  HMAC_SHA256(secret, signing_string) 的十六进制
```

待签名串（换行连接）：

```
<key_id>\n<timestamp>\n<nonce>\n<METHOD>\n<path>\n<SHA256(body) 的 hex>
```

- 验签使用 `hmac.compare_digest`（常量时间比较，防时序侧信道）；
- 缺失头、错误 key、超窗、nonce 重放、签名不符一律 `401`；
- 每个成功响应带 `X-Response-Signature`（对响应体的 HMAC），客户端可验真；
- 每条分配返回 `receipt`：对全部关键结算字段的规范 JSON（排序键）做 HMAC，
  可用 `app.crypto.verify_receipt` 离线验真；
- 默认密钥仅用于本地演示，生产必须用环境变量覆盖：
  `DISPATCH_API_KEY_ID` / `DISPATCH_API_SECRET`。

环境变量一览：`DISPATCH_DB_PATH`、`DISPATCH_BASE_RATE`、
`DISPATCH_LOAD_RATE`、`DISPATCH_WAIT_RATE`、`DISPATCH_SAFETY_MARGIN_WH`、
`DISPATCH_MEASUREMENT_TOLERANCE`、`DISPATCH_TIMESTAMP_WINDOW_S`。

---

## 6. 本地启动

需要 Python 3.10+（开发环境为 3.12）。

```bash
cd P070/b
python3 -m venv .venv
source .venv/bin/activate

# 可复现安装（锁定版本）
pip install -r requirements.lock
# 或安装直接依赖
# pip install -r requirements.txt

# 可选：写入示例种子数据
python -m scripts.seed

# 启动
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

---

## 7. 验收命令

### 7.1 自动化测试（33 个，全部必须通过）

```bash
source .venv/bin/activate
python -m pytest -v
```

覆盖（与需求逐条对应）：

- `tests/test_energy.py`：距离/载荷/等待公式、安全余量边界；
- `tests/test_dispatch.py`
  - **局部最便宜但无法返航**被拒；
  - **充电点竞争**（单充电点先占先得 + 同批次重算到其他充电点）；
  - 数据库唯一索引直接拦截双重预占；
  - **取消预占**释放电量/充电点并能重新分配；
  - 重复接单 409、一台机器人不能接两单；
  - 最小成本选择正确、全部候选边可解释、回执 HMAC 可验真；
  - 控制指令为 `simulated`；
- `tests/test_failure_and_telemetry.py`
  - 充电点失效：planned 释放重算、running 保留 + critical 告警；
  - **实测电量低于预测**告警、正常偏差不告警；
- `tests/test_crypto.py`：HMAC 真伪向量、篡改拒绝、时间窗、nonce 防重放、
  响应签名验真。

### 7.2 端到端演示（真实 HMAC，无需先启动服务）

```bash
python -m scripts.demo
```

或对已启动的真实 HTTP 服务：

```bash
python -m scripts.demo http://127.0.0.1:8000
```

### 7.3 curl 手工验收

```bash
# 终端 A
uvicorn app.main:app --port 8000

# 终端 B
scripts/signed_curl.sh POST /api/robots   examples/robot.json
scripts/signed_curl.sh POST /api/chargers examples/charger.json
scripts/signed_curl.sh POST /api/tasks    examples/task.json
scripts/signed_curl.sh POST /api/dispatch examples/dispatch.json
scripts/signed_curl.sh GET  /api/snapshot
scripts/signed_curl.sh POST /api/chargers/C1/fail examples/charger_fail.json
scripts/signed_curl.sh GET  /api/alerts
```

---

## 8. 目录结构

```
app/
  config.py      环境变量与模型常量
  crypto.py      HMAC-SHA256 签名/验签/回执（标准库真实密码操作）
  energy.py      显式能耗公式与可达性判定（纯函数）
  database.py    SQLite schema、唯一索引、事务
  schemas.py     Pydantic 模型
  services.py    匹配/预占/取消/失效重算/遥测（事务化领域服务）
  main.py        FastAPI 路由与签名中间件
scripts/
  seed.py          示例数据
  demo.py          端到端演示（真实签名 + 回执验真）
  signed_curl.sh   签名 curl 辅助
tests/             pytest 自动化测试（33 个）
examples/          请求体示例
requirements.txt   直接依赖
requirements.lock  锁定依赖（pip freeze，含传递依赖版本）
```

## 9. 已知边界（合成系统的诚实声明）

- 机器人位置在任务 `complete` 时结算到充电点，执行中途不连续运动学仿真；
  实测电量通过遥测接口进入系统，偏差告警即用于暴露「公式预测 ≠ 真实耗电」。
- 不做全局（匈牙利）最优，而采用按成本升序的贪心匹配——它是确定性、
  可逐条解释的，并在响应中暴露全部候选边供审计；算法名已版本化，
  可在不破坏协议的前提下替换。
- 所有"控制命令"均为合成数据，系统没有任何向真实设备发送指令的代码路径。
