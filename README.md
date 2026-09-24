# Battery-Reachable Task Allocation (P070)

纯后端服务：合成机器人（synthetic robots）任务分配。分配时用**显式公式**计算
距离、载荷、等待时长的电量消耗，并保证机器人完成任务后**必须能到达任一可用充电点
且保留安全余量**；多机器人采用可解释的全局**最小成本匹配（匈牙利算法）**；
分配与电量预占在**同一 SQLite 事务**内完成；机器人不能重复接任务；充电点失效后
重算所有未开始任务，正在运行的任务保留并发出风险告警。

> 本服务是规划/演示后端：**不输出任何真实机器人控制命令**。"下发"仅在
> `dispatch_logs` 中写一条仿真记录，日志与 API 响应均带
> `"simulated": true` 标记。

## 1. 目录结构

```
app/
  config.py        常量与安全余量
  crypto.py        PBKDF2 口令哈希 + HMAC-SHA256 签名令牌（真实密码学操作）
  energy.py        显式电量/成本公式、返航可达性
  matching.py      匈牙利算法（O(n^3)，通用实现）
  database.py      SQLite 连接、建表
  schemas.py       Pydantic 请求/响应模型
  services.py      事务化业务逻辑（分配、取消、测量、失效重算…）
  api.py           FastAPI 路由
  main.py          应用入口
examples/
  seed_demo.py     幂等示例数据
  demo.sh          curl 端到端演示
  acceptance.py    一键验收脚本（与 pytest 相互独立）
tests/             自动化测试
requirements.txt   锁定依赖
```

## 2. 本地启动

```bash
cd /home/admin/Downloads/biaozhul/P070/a
python3 -m venv .venv && source .venv/bin/activate   # 可选
pip install -r requirements.txt

export DISPATCH_SECRET="$(openssl rand -hex 32)"     # 可选，不设则用开发默认值
python -m examples.seed_demo --reset                # 写入示例输入（演示账号）
uvicorn app.main:app --reload
# API: http://127.0.0.1:8000  文档: /docs  健康检查: /health
```

演示操作员账号：`operator` / `demo-password-123`（仅 seed 创建，生产请改）。
登录拿到 `access_token`，写接口需要请求头 `X-Auth-Token: <token>`。

## 3. 验收命令

```bash
# 方式 A：pytest 全部自动化测试（含 4 个规定场景，85 个用例）
pip install -r requirements.txt
pytest -q

# 方式 B：独立验收脚本（对运行中的服务做端到端不变量检查；
#          场景放在 y=100000 的隔离走廊，不受 seed 数据影响）
uvicorn app.main:app --port 8070 &
python -m examples.acceptance --base-url http://127.0.0.1:8070

# 方式 C：curl 端到端演示（自包含：自动用临时库起服务、结束自动清理）
bash examples/demo.sh        # PORT=xxxx 可改端口
```

## 4. 规定测试场景与验收结果

| 场景 | 测试 | 期望 |
|---|---|---|
| 局部最便宜但无法返航 | `test_01_locally_cheapest_but_cannot_return` | 贪心会选中便宜但回不了充电点的机器人；匈牙利分配返回 `unreachable`，任务改派可行机器人或不分配 |
| 充电点竞争 | `test_02_charger_competition` | 两台机器人争唯一充电点：拿到预留的成功，另一台改派次优可达充电点，无可达点则进入 `unassigned` |
| 取消预占 | `test_03_cancel_releases_reservation` | 取消后机器人回到 idle、电量预占释放、充电点预留释放，可立即接新任务 |
| 测量电量低于预测 | `test_04_measured_battery_below_prediction` | 低于临界：critical 告警且禁止开始；轻度偏低：warning；高于预测：OK |
| 充电点失效（附加） | `test_05_charger_failure_recompute` | 未开始任务自动重算/释放，运行中任务保留并产生 risk 告警 |
| 密码操作（附加） | `test_06_signed_token_tampering` | HMAC 令牌被篡改/过期一律拒绝，PBKDF2 校验真实执行 |

## 5. 显式计算模型

平面欧氏距离 `d = hypot(Δx, Δy)`（单位 m）。一次任务三段行程：
去程（机器人当前位置→取货）、载货（取货→交付）、返航（交付→充电点）。

```
leg_energy(segment_m, payload_kg, loaded)
    = segment_m * (base_consumption + payload_factor * effective_payload)
其中 effective_payload = payload_kg（载货段），0（空驶段）

wait_energy   = wait_s / 60 * idle_per_min     （等待/空转耗能）
mission_energy = leg(去程) + leg(载货) + wait_energy + leg(返航)
required_soc   = mission_energy + safety_margin
safety_margin  = margin_fraction × capacity    （默认 10% 电池容量）
reachable      ⇔ current_soc >= required_soc 对至少一个可用充电点成立
```

默认参数（`app/config.py`，可按机器人覆盖部分字段）：

- `base_consumption = 0.020` kWh/m
- `payload_factor  = 0.0008` kWh/(m·kg)
- `idle_per_min    = 0.05` kWh/min

匹配成本（同量纲，均可向操作员解释）：

```
cost = mission_energy * energy_price          # 电量成本
     + total_distance * distance_price        # 行驶/磨损成本
     + wait_s/60 * wait_price                 # 等待时间成本
     + priority_bonus * (max_priority - task_priority)  # 高优先级优先
```

匹配结果中的 `explanation` 给出每个机器人每个任务的距离、各段耗能、
最近可达充电点、总成本；每条已提交分配附带 `rejected_alternatives`
（其他机器人为何落选），未分配任务给出原因（`insufficient_battery` /
`no_reachable_charger` / `blocked_by_charger_contention` / `robot_not_idle`…）。

## 6. 一致性保证

- 分配在单个 SQLite 事务中：写 `assignments` + 置机器人 `reserved` +
  预占电量（`reserved_soc`）+ 预占充电点，任一失败整体回滚。
- 机器人状态机 `idle → reserved → running → charging/done`，
  部分唯一索引保证一个机器人至多一条活跃任务、一个任务至多被分配一次。
- 充电点 `available` 槽位按 ETA 顺序在匹配轮次中预占，杜绝双重预订。
- 充电点失效：未开始分配在同一事务内撤销，随后对受影响任务重算；
  运行中的分配不动，写 risk 告警（含备选可达充电点的真实测算）。

## 7. API 摘要

```
POST /auth/register  POST /auth/login                 操作员注册/登录
GET  /robots  POST /robots        GET/PUT/DELETE /robots/{id}
GET  /chargers POST /chargers     POST /chargers/{id}/failover
GET  /tasks   POST /tasks         GET/DELETE /tasks/{id}
POST /tasks/preview-cost                        成本/可达性试算（不落库）
POST /dispatch/assignments/batch                 全局最小成本匹配 + 事务预占
                                                 （可用 ?task_ids=T1&task_ids=T2 限定范围）
GET  /assignments            POST /assignments/{id}/start|complete|cancel
POST /assignments/{id}/telemetry                 上报实测电量
POST /assignments/{id}/unplug                    充完释放充电点
GET  /alerts
GET /dispatch/logs
GET /health
```

## 8. 安全说明

- 口令：PBKDF2-HMAC-SHA256，240k 轮，每用户随机 16 字节盐，恒定时间比较。
- 令牌：`HMAC-SHA256(payload_b64, server_secret)`，载荷含操作员 id 与过期时间，
  改一个字节签名即失效；密钥来自 `DISPATCH_SECRET` 环境变量。
- 所有写接口需要有效令牌；只读 GET 接口在演示环境开放。
- 密码学操作全部由标准库 `hashlib` / `hmac` / `secrets` 真实执行，无桩实现。
