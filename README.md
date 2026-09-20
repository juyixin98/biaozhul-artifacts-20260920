# MLOps 本地资源调度引擎

基于 **Django REST Framework + MySQL + Docker Compose** 的本地 GPU 资源调度引擎。
仅**模拟** GPU 资源（按卡数与单卡显存记账）与作业状态，不含真实训练或推荐系统。

实现：节点上报心跳与硬件、资源池配额、装箱调度、优先级抢占、排队/运行超时、
失联检测、重启恢复、全程决策审计；多个调度器并发运行也**不超卖、不重复分配、
不越过资源池配额**。

---

## 1. 目录结构

```
config/                  Django 工程配置（settings/urls/wsgi）
scheduler/
  models.py              资源池 / 节点 / 作业 / 分配关系 / 决策与抢占审计
  packing.py             GPU 装箱（Best-Fit）与作业排序规则（纯函数，可单测）
  engine.py              调度引擎：配额、装箱分配、抢占（两阶段+故障回滚）
  reconcile.py           幂等结算、排队/运行超时、心跳/失联/恢复、启动恢复
  locking.py             资源池调度互斥锁（MySQL 咨询锁；SQLite 进程内锁）
  clock.py               可注入时钟（Clock / FakeClock）
  serializers.py / views.py / urls.py   REST API
  management/commands/
    run_scheduler.py      后台调度循环（失联检测→运行超时→逐池调度）
    recover_state.py      启动恢复对账
    seed_demo.py          演示数据
    fake_node_heartbeat.py 模拟节点心跳进程
  tests/
    test_packing.py            装箱与排序规则
    test_scheduler.py          调度/配额/抢占/超时/失联/恢复（33 个）
    test_concurrency_mysql.py  多连接并发竞态（7 个，需 MySQL）
docker-compose.yml       db + migrator + api + scheduler
Dockerfile / entrypoint.sh
```

---

## 2. 快速开始（Docker Compose）

```bash
# 默认 MySQL 3306、API 8000；如端口被占用可覆盖：
MYSQL_PORT=3307 API_PORT=8080 docker compose up -d --build

# 容器：
#   db        MySQL 8.4（健康检查）
#   migrator  一次性独占执行 migrate（避免多服务并发迁移冲突）
#   api       gunicorn :8000（迁移+恢复后启动）
#   scheduler 后台调度循环（每 3s：失联检测→运行超时→逐池调度）
```

初始化演示数据并模拟一个节点心跳：

```bash
docker compose exec api python manage.py seed_demo
docker compose exec api python manage.py fake_node_heartbeat node-a1 &
```

停止：`docker compose down`（保留数据卷）；连数据一起清：`docker compose down -v`。

> 也可只起数据库、在本机运行服务：
> ```bash
> MYSQL_PORT=3307 docker compose up -d db
> python -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
> export MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307
> python manage.py migrate
> python manage.py runserver 127.0.0.1:8000
> python manage.py run_scheduler            # 另一个终端
> ```

不使用 MySQL 也可用 SQLite 跑功能测试与本地体验：
`DB_ENGINE=sqlite python manage.py test scheduler`

---

## 3. 数据模型与状态机

| 模型 | 关键字段 | 说明 |
|---|---|---|
| `ResourcePool` | `max_gpus`、`max_running_jobs` | GPU 总配额（0 不限）与最大**并发活动作业数**（0 不限） |
| `Node` | `gpu_count`、`gpu_memory_mb`、`status`、`last_heartbeat_at` | 节点：GPU 数量、**单卡显存**、状态、心跳 |
| `Job` | `min_gpus`/`max_gpus`、`gpu_memory_mb`、`priority(1–10)` | 作业：GPU 弹性区间、单卡显存要求、优先级 |
| `Allocation` | `job`、`node`、`gpu_count` | 占用明细，是**重启恢复的事实来源**（已用量实时聚合，不存计数器） |
| `SchedulingDecision` | `kind`、`rationale(JSON)`、`success` | 每次调度/跳过/抢占/超时/失联/恢复的依据 |
| `PreemptionRecord` | `preemptor`、`victim`、`success`、`detail` | 每次抢占的结果（成功/失败及原因） |

**节点状态**：`available` 可用 / `occupied` 占用 / `draining` 排空 / `offline` 离线。
只有 `available`、`occupied` 进入调度候选；`draining` 不接新任务（存量作业保留）；
`offline` 上的资源**不会被当成可用**。

**作业状态**：`queued → allocated → running → succeeded`，旁路
`cancelled / timed_out / failed / preempted`。`allocated/running` 为持有 GPU 的
活动态，其余为终态。

---

## 4. 调度规则

### 4.1 同优先级排序
每轮按 `(-priority, queued_at, id)` 排序扫描排队作业：

1. **优先级高者先调度**（10 最先，1 最后）；
2. **同优先级 FIFO**：先入队先得；
3. `id` 兜底，保证结果完全确定、可复现。

### 4.2 装箱（Best-Fit Decreasing）
对每个作业，只取可调度且 `节点单卡显存 ≥ 作业要求` 的节点：

1. 候选按 **空闲卡数升序**——优先塞进“最小能装下”的节点，把大块连续空间
   留给需要整节点的大作业，**降低碎片**；同空闲量时显存小的先用（留大显存给
   高显存需求）；再以节点 id 稳定排序；
2. 从最紧凑节点起尽量取其全部空闲卡，直到凑到 `max_gpus`；
3. 允许跨节点拆分；只要凑到 `≥ min_gpus` 即分配（弹性区间），
   实际卡数取 `min(max_gpus, 总空闲)`；
4. 否则保持排队，并在 `SchedulingDecision` 中记录原因与候选快照
   （总量不足 / 显存不足 / 无可调度节点）。

### 4.3 资源池配额
- 分配后池内（可调度节点上的）占用总量不得超过 `pool.max_gpus`；
- 活动作业数不得超过 `pool.max_running_jobs`。
配额检查与占用写入在同一事务完成。

---

## 5. 并发安全（多个调度器并存）

1. **每池一把调度咨询锁**（`GET_LOCK('mlops_sched_pool_<id>')`，跨进程/跨连接）。
   同一资源池的调度循环全局串行；拿不到锁的副本本轮**直接跳过**，绝不绕锁分配。
2. 事务内对 `resource_pool / node / job` 行 `SELECT … FOR UPDATE`，并以
   **状态条件更新（CAS）** 兜底，固定“先池锁、后行锁”的加锁顺序。
3. GPU 已用量不保存计数器，每次由 `Allocation` 实时 `SUM` 聚合，杜绝计数漂移。

因此并发下满足：**不超卖 GPU、同一作业不重复分配、不越过池 GPU/并发配额**。
（SQLite 用进程内可重入锁模拟，便于无 MySQL 时跑功能测试；生产用 MySQL。）

---

## 6. 抢占（优先级 ≥8 抢占 ≤3）

- 仅 `priority >= 8` 的排队作业可抢占，且受害者必须 `priority <= 3`、同池活动；
- 受害者选择：**优先级最低 → 占用卡数最少 → id** 排序，选“满足释放条件的最少集合”
  （同时考虑 GPU 缺口与并发名额缺口）；
- **先确认释放、再重新分配**：在同一事务的 SAVEPOINT 中
  「CAS 抢占受害者 → 删除其分配 → 重新装箱 → 写入新分配」。
  若重新分配阶段失败（或被注入故障），SAVEPOINT 回滚，**受害者复活、资源不泄漏**，
  调度继续处理其它作业；下次调度可再次尝试（失败可恢复）；
- 全过程写 `SchedulingDecision(preempt_attempt/preempt_result)` 与
  `PreemptionRecord`（含成功与否、释放卡数、失败原因）。

---

## 7. 超时、失联与恢复

- **排队超时**：排队超过 2 小时（`QUEUE_TIMEOUT_SECONDS`，可调）的作业自动置
  `timed_out`；
- **运行超时**：活动作业超过自身 `run_timeout_seconds`（缺省用
  `DEFAULT_RUN_TIMEOUT_SECONDS`）置 `timed_out`；
- **心跳失联**：节点超过 `HEARTBEAT_TIMEOUT_SECONDS`（默认 30s）无心跳则置
  `offline`，其上活动作业判 `failed` 并释放占用；恢复前这些卡不进调度候选；
- **心跳恢复**：`offline` 节点重新上报心跳即回到 `available`；
- **排空**：`draining` 不接新任务，存量作业跑完；
- **结算幂等**：完成/取消/超时/失败并发时，`settle_job()` 用 CAS 保证资源
  **只结算一次**，重复调用返回 409 且不重复释放；
- **重启恢复**：`Allocation` 表即事实来源，`recover_on_startup()` 无需重建计数，
  只按占用对账节点 `available/occupied` 状态（容器 entrypoint 启动时自动执行）。

---

## 8. 可注入时钟

业务代码统一从 `scheduler.clock.get_clock()` 取时间，不直接读系统时钟。
测试注入 `FakeClock` 任意推进时间，确定性验证 2 小时超时、心跳失联、运行超时：

```python
from scheduler.clock import FakeClock, set_default_clock
clock = FakeClock()
engine.schedule_pool(pool.id, clock=clock)
clock.advance(2 * 3600 + 1)   # 推进到排队超时之后
```

---

## 9. REST API（前缀 `/api`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/pools/` | 资源池列表/创建 |
| POST | `/pools/{id}/schedule/` | 触发该池一轮调度（返回分配/跳过/抢占/超时） |
| POST | `/pools/schedule-all/` | 调度所有池 |
| GET/POST | `/nodes/` | 节点列表/注册 |
| POST | `/nodes/{id}/heartbeat/` | 上报心跳（可带 `gpu_count`、`gpu_memory_mb`） |
| POST | `/nodes/{id}/drain/` `/activate/` | 排空 / 恢复 |
| POST | `/nodes/detect-stale/` | 手动执行一次失联检测 |
| GET/POST | `/jobs/` | 作业列表/提交（body 见下） |
| POST | `/jobs/{id}/start/` | `allocated → running`（模拟训练启动） |
| POST | `/jobs/{id}/complete/` `/cancel/` | 完成 / 取消（幂等结算） |
| GET | `/decisions/` | 调度决策审计（可按 `pool/job/node/kind/success` 过滤） |
| GET | `/preemptions/` | 抢占记录（可按 `pool` 过滤） |

提交作业 body：

```json
{
  "pool": 1,
  "name": "train-xxx",
  "min_gpus": 2,
  "max_gpus": 8,
  "gpu_memory_mb": 16384,
  "priority": 9,
  "run_timeout_seconds": 3600
}
```

### 端到端示例

```bash
# 资源池：12 卡、并发 3
curl -s -X POST localhost:8000/api/pools/ -H 'Content-Type: application/json' \
  -d '{"name":"p1","max_gpus":12,"max_running_jobs":3}'
# 节点：8 张 32GB 卡
curl -s -X POST localhost:8000/api/nodes/ -H 'Content-Type: application/json' \
  -d '{"pool":1,"name":"n1","gpu_count":8,"gpu_memory_mb":32768}'
curl -s -X POST localhost:8000/api/nodes/1/heartbeat/ -d '{}'
# 作业 + 调度
curl -s -X POST localhost:8000/api/jobs/ -H 'Content-Type: application/json' \
  -d '{"pool":1,"name":"j1","min_gpus":2,"max_gpus":4,"gpu_memory_mb":16384,"priority":5}'
curl -s -X POST localhost:8000/api/pools/1/schedule/
curl -s "localhost:8000/api/decisions/?job=1"     # 查看调度依据
```

---

## 10. 测试

```bash
# 功能/规则测试（SQLite 即可，33 个）
DB_ENGINE=sqlite python manage.py test scheduler.tests.test_packing scheduler.tests.test_scheduler

# 多连接并发竞态测试（需 MySQL，共 40 个）
MYSQL_HOST=127.0.0.1 MYSQL_PORT=3307 python manage.py test scheduler
```

并发测试覆盖（`scheduler/tests/test_concurrency_mysql.py`）：
- **并发调度**：4 个调度器线程 × 多轮，验证 16 卡池恰好分配 16/32 个作业、
  无节点超容量、无重复分配；
- **并发配额**：并发上限在竞争下不被突破；
- **取消/结算竞态**：完成、取消、超时三方并发，恰有一方成功、资源只释放一次；
- **抢占失败恢复**：注入故障使“释放后重新分配”失败 → 回滚受害者复活、资源不
  泄漏；去掉故障后重试成功；
- **节点失联恢复**：失联检测与调度并发，失联卡不被新作业使用；心跳恢复后可再调度；
- **排队超时竞态**：两个调度器并发，超时取消与审计只发生一次。

测试中若用户无建库权限，需对测试库授权（仅业务库与测试库的最小授权）：

```sql
GRANT ALL PRIVILEGES ON `mlops_scheduler`.* TO 'scheduler'@'%';
GRANT ALL PRIVILEGES ON `test_mlops_scheduler`.* TO 'scheduler'@'%';
```

---

## 11. 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DB_ENGINE` | `mysql` | 设 `sqlite` 切换 SQLite |
| `MYSQL_HOST/PORT/DATABASE/USER/PASSWORD` | 127.0.0.1/3306/mlops_scheduler/scheduler/scheduler_pw | MySQL 连接 |
| `HEARTBEAT_TIMEOUT_SECONDS` | 30 | 心跳失联阈值 |
| `QUEUE_TIMEOUT_SECONDS` | 7200 | 排队超时（2 小时） |
| `DEFAULT_RUN_TIMEOUT_SECONDS` | 21600 | 缺省运行超时 |
| `API_PORT` / `MYSQL_PORT`（compose） | 8000 / 3306 | 宿主机端口映射 |
| `GUNICORN_WORKERS` / `SCHED_INTERVAL` | 3 / 3 | API worker 数 / 调度循环间隔 |
