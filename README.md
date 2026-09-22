# MLOps 本地资源调度引擎（GPU 调度模拟）

使用 **Django REST Framework + MySQL + Docker Compose** 实现的本地 GPU 资源调度引擎。
**只模拟 GPU 资源和作业状态**——没有真实训练，也没有推荐系统。节点由内置的
“模拟节点 agent”（通过 REST API 上报心跳、完成作业）扮演。

---

## 1. 领域模型

| 模型 | 说明 |
| --- | --- |
| `ResourcePool` | 资源池。配额：`max_total_gpus`（池内 GPU 总量上限）、`max_concurrent_jobs`（并发作业数上限）。 |
| `Node` | GPU 节点。上报 `gpu_count`（GPU 数量）、`gpu_memory_mb`（单卡显存）、心跳时间。维护 `allocated_gpus` 计数器。 |
| `Job` | 作业。申请 `min_gpus`/`max_gpus`、单卡显存 `gpu_memory_mb`、优先级 `priority`（1–10）。 |
| `Allocation` | **权威的“作业 X 占用节点 Y 上 n 张 GPU”记录**，一一对应一次活动分配；删除即归还 GPU，也是重启后恢复计数的依据。 |
| `PreemptionRequest` / `PreemptionVictim` | 一次抢占尝试及其被抢占作业（含 GPU 数量快照），状态 `PENDING / RELEASED / FAILED`。 |
| `SchedulingDecision` | 每次调度/抢占/结算的**不可变审计记录**（依据、候选节点、受害者、原因 JSON）。 |

### 节点状态（有效状态由“管理状态 + 心跳新鲜度”实时推导）

- `AVAILABLE`：在线、心跳新鲜、有空闲 GPU
- `OCCUPIED`：在线、心跳新鲜、GPU 已被部分占用
- `DRAINING`：**排空中**——已有作业继续运行，但不接新任务
- `OFFLINE`：管理员下线，或心跳超时失联（失联节点上的资源**绝不**当作可用）

### 作业状态

`PENDING → RUNNING → COMPLETED`；`RUNNING/PENDING → CANCELLED`（用户取消 / 排队 2 小时超时 /
被抢占）；运行中节点失联 → `FAILED (NODE_LOST)`；抢占通知后进入 `PREEMPTING`，
等 worker 真正释放 GPU。

---

## 2. 调度规则

每次调度 `tick` 对每个资源池在**一个事务**里按固定顺序执行四个阶段：

1. **节点存活检测**：心跳超时 → 标记 OFFLINE，节点上存活作业以 `NODE_LOST` 各结算一次。
2. **排队超时**：`PENDING` 超过 `QUEUE_TIMEOUT_SECONDS`（默认 7200 秒 / 2 小时）取消为 `TIMEOUT`。
3. **抢占跟进**：确认被抢占作业是否**真的已释放 GPU**；释放成功才置 `RELEASED`，
   超过 `PREEMPT_GRACE_SECONDS`（默认 30s）未释放则平台强制回收；目标节点失联 /
   受益作业消失则置 `FAILED`。
4. **放置**：装箱分配；放不下且优先级 ≥ 8 时发起抢占。

### 装箱（bin packing）

- 作业**只能落在单台节点内**获得连续 GPU（符合常见单机训练 / 多卡 NCCL 约束），
  分配数量取 `min(max_gpus, 节点空闲数)`，且必须 ≥ `min_gpus`。
- **Best-fit（最紧装箱）**：在所有满足显存与最小 GPU 数的节点中，选择分配后
  **剩余空闲 GPU 最少**的节点（把大节点的大块连续容量留给大作业，减少碎片）；
  剩余相同按 `node.id` 升序确定。
- 单卡显存不足的节点直接排除。

### 同优先级排序

候选作业按 `priority DESC, enqueued_at ASC, id ASC` 排序——**优先级高的先调度，
同优先级严格 FIFO**。

### 资源池配额

- 节点（重新）注册时校验：不能让池内 GPU 总量超过 `max_total_gpus`；节点不能缩容到
  已分配 GPU 数之下。
- 每次放置还会再做一次池配额与 `max_concurrent_jobs` 并发数校验。

---

## 3. 抢占（两阶段，先确认释放再重分配）

- 规则：仅当候选作业 **优先级 ≥ 8**，才允许抢占目标节点上 **优先级 ≤ 3** 的 `RUNNING` 作业。
  优先级 4–7 的作业不可被抢占；优先级 7 及以下的作业本身也不能发起抢占。
- 受害者选择：贪心按 GPU 数降序（尽量少打扰作业），优先级升序、id 升序；
  在可行节点中选择“受害者最少 → 回收 GPU 最少 → 节点 id 最小”的方案。
- **阶段 1（发起）**：受害者 `RUNNING → PREEMPTING`，生成 `PreemptionRequest(PENDING)`，
  记录审计依据。此时**不做任何重分配**。
- **阶段 2（确认）**：worker 调用 `/api/jobs/{id}/release/`（或 grace 到期平台强制回收），
  等**每个受害者的 Allocation 行都消失**后请求才置 `RELEASED`；之后的放置阶段才把受益作业
  调度到该节点。因此“先释放、再分配”是跨 tick、甚至跨重启的硬保证。
- **失败恢复**：等待期间目标节点失联 → 请求 `FAILED (target_node_lost)`，受害者 `FAILED`，
  受益作业保持 `PENDING`，后续在其它节点上重试；受益作业被取消 → 仍把受害者释放干净后
  `FAILED (beneficiary_not_pending)`，不泄漏 GPU。

---

## 4. 并发正确性

- 每个池在 tick 开始时对 **resource_pool 行 `SELECT … FOR UPDATE`** 加锁，因此可以
  **同时运行多个调度器进程**：同池的 tick 被串行化，绝无 GPU 超卖 / 重复分配 / 越过配额；
  不同池之间仍完全并行。
- 全局加锁顺序统一为 `resource_pool → nodes(id 升序) → jobs(id 升序)`，
  避免调度器与 API（完成 / 取消 / 心跳 / 管理状态）之间死锁。
- 每次作业终结都是**条件 UPDATE（CAS）**：完成、取消、超时、心跳恢复并发发生时，
  只有一个赢家，资源只结算一次。
- 节点 GPU 计数的增减全部是锁定行上的原子 SQL（`F()` / `Greatest(0, …)`），
  不用可能过期的 Python 快照覆盖。
- **失联节点的容量不参与放置**；心跳恢复时以 Allocation 表自愈计数器。
- **重启恢复**：所有状态在 MySQL。启动时 `recover_from_database()` 依据 Allocation 行
  重建节点计数；未完成的 `PENDING` 抢占请求继续沿用两阶段流程。

### 可注入时钟

`scheduler/clock.py` 提供 `Clock`（生产读 `django.utils.timezone.now()`）和 `FakeClock`。
测试里全局 patch `timezone.now`，模型的 `auto_now*` 时间戳也受同一时钟控制，因此
2 小时排队超时、心跳超时、抢占 grace 全部可确定性测试（含 worker 线程内）。

---

## 5. 快速开始

### 5.1 Docker Compose（推荐，MySQL）

```bash
cp .env.example .env

# 启动 MySQL + Web API + 常驻调度器
docker compose up -d --build

# （可选）启动两个模拟 GPU 节点
docker compose --profile mock up -d

# 准备一个演示池 + 两个节点 + 一批作业
docker compose exec web python manage.py seed_demo

# 观察调度器日志（每 2 秒一轮）
docker compose logs -f scheduler
```

服务：

- Web/API：<http://localhost:8000/>
- 手动触发一轮调度：`POST http://localhost:8000/api/scheduler/tick/`
- MySQL：`localhost:3306`（库 `mlops` / 用户 `mlops` / 密码见 `.env`）

### 5.2 本地裸跑（SQLite，无需 Docker）

```bash
pip install -r requirements.txt        # mysqlclient 本地非必需
python manage.py migrate
python manage.py seed_demo             # 建池/节点/作业
python manage.py run_scheduler --once  # 单轮调度
# 另开终端：
python manage.py runserver
python manage.py mock_node --hostname node-a --pool demo --gpus 8
```

### 5.3 端到端手动脚本

```bash
curl -s localhost:8000/api/pools/ -H 'Content-Type: application/json' \
  -d '{"name":"demo","max_total_gpus":8,"max_concurrent_jobs":4}'
curl -s localhost:8000/api/nodes/register/ -H 'Content-Type: application/json' \
  -d '{"pool":"demo","hostname":"n1","gpu_count":4,"gpu_memory_mb":81920}'
curl -s localhost:8000/api/nodes/heartbeat/ -H 'Content-Type: application/json' \
  -d '{"hostname":"n1"}'
curl -s localhost:8000/api/jobs/ -H 'Content-Type: application/json' \
  -d '{"pool":1,"name":"t1","min_gpus":1,"max_gpus":2,"gpu_memory_mb":1024,"priority":5}'
curl -s -X POST localhost:8000/api/scheduler/tick/
curl -s 'localhost:8000/api/decisions/?limit=20'
```

---

## 6. HTTP API 摘要

| 方法/路径 | 作用 |
| --- | --- |
| `GET/POST /api/pools/`，`GET/PATCH /api/pools/{id}/` | 资源池及配额（收缩到已用量以下返回 409） |
| `GET /api/nodes/` | 节点列表（含推导的 `state`、`free_gpus`） |
| `POST /api/nodes/register/` | 节点上报 GPU 数量、单卡显存、归属池（超配额 409） |
| `POST /api/nodes/heartbeat/` | 节点心跳（含失联自愈/恢复判定） |
| `POST /api/nodes/{id}/state/` | 设置 `ONLINE / DRAINING / OFFLINE` |
| `GET/POST /api/jobs/` | 作业列表/提交（min≤max、优先级 1–10 校验） |
| `POST /api/jobs/{id}/cancel/` | 用户取消（幂等：重复取消 409） |
| `POST /api/jobs/{id}/complete/` | worker 上报完成并归还 GPU |
| `POST /api/jobs/{id}/release/` | worker 确认抢占、已释放 GPU |
| `GET /api/allocations/` | 当前活动分配（可按 pool/node 过滤） |
| `GET /api/preemptions/` | 抢占请求及受害者（可按 status 过滤） |
| `GET /api/decisions/` | 审计依据（可按 kind/job 过滤） |
| `POST /api/scheduler/tick/` | 手动跑一轮调度（返回各阶段计数） |

模拟节点 agent（`manage.py mock_node`）只用公开 REST API：注册 → 心跳 → 轮询自身分配 →
作业跑满 `--job-lifetime` 后 complete；发现作业变 `PREEMPTING`，等待
`--preempt-delay` 后调用 release。

---

## 7. 管理命令

```bash
python manage.py run_scheduler [--interval 2] [--once]  # 常驻调度循环 / 单轮
python manage.py mock_node --hostname node-a --pool demo \
    --gpus 8 --gpu-memory 81920 --interval 2 \
    --job-lifetime 60 --preempt-delay 2
python manage.py seed_demo [--pool demo]
```

`run_scheduler` 启动时先做一次数据库恢复；可安全地多副本运行（靠池行锁串行化）。

---

## 8. 测试

覆盖题目要求的五类场景，另含 API 端到端与重启恢复：

- **并发调度**：6–8 个调度器线程同时 tick，校验不超卖、不重复分配、并发结算只一次。
- **资源碎片**：聚合空闲够但无单节点可放时必须等待；best-fit 减少碎片。
- **抢占失败**：目标节点失联 → 请求失败、受益作业在别处恢复；grace 超时强制回收；
  受益作业取消仍释放干净；受害者抢先完成的竞态。
- **节点失联恢复**：心跳超时下线与作业 NODE_LOST、心跳恢复、管理员下线需显式恢复。
- **取消竞态**：完成 vs 取消、完成 vs tick 并发，只有一个赢家且只结算一次。

本地（SQLite 文件库，无需 MySQL）：

```bash
ML_SCHED_DB=/tmp/sched_test.sqlite3 python manage.py test scheduler --noinput
```

在 Docker / MySQL 上验证真实行锁并发（测试库需要建库权限，故用 root 连接；
生产/运行用受限的 `mlops` 用户）：

```bash
# 若 3306 已被占用，可加 -f <(printf 'services:\n  db:\n    ports: !reset []\n')
docker compose up -d db
docker compose build web
docker compose run --rm \
  -e DB_ENGINE=mysql -e DB_HOST=db \
  -e DB_USER=root -e DB_PASSWORD=root_password \
  web python manage.py test scheduler --noinput
```

> 说明：并发测试继承 `TransactionTestCase`，worker 线程使用各自独立的数据库连接；
> MySQL 上由 `FOR UPDATE` 保证，SQLite 文件库上由写事务串行化给出相同的外部可观测性质。

---

## 9. 关键文件

```
scheduler/
  models.py        # 领域模型与状态推导（effective_state / 心跳新鲜度）
  clock.py         # 可注入时钟
  services.py      # 注册/心跳/管理状态/作业结算/重启恢复（CAS + 原子计数）
  engine.py        # tick 四阶段、best-fit 装箱、两阶段抢占
  views.py urls.py serializers.py   # REST API
  management/commands/
    run_scheduler.py  mock_node.py  seed_demo.py
  migrations/0001_initial.py
  tests/           # scheduling / preemption / resilience / concurrency / api
mlops_engine/      # Django 工程（settings 支持 mysql|sqlite 环境切换）
docker-compose.yml Dockerfile entrypoint.sh requirements.txt
```
