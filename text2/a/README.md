# CareForce 护理任务排班后端

基于 FastAPI + SQLAlchemy + PostgreSQL + Alembic 的护理任务排班服务。
范围仅包含：**任务生成、约束分配、超时重排**。不含薪资、志愿者或医疗决策。

## 快速启动（Docker）

```bash
docker compose up --build
```

应用启动时自动执行 `alembic upgrade head`，然后监听 http://localhost:8000 。
交互式 API 文档：http://localhost:8000/docs （Swagger UI）。

可选：写入样例数据（单元、护理员、协调员、两个计划并生成 14 天任务）：

```bash
docker compose exec app python -m app.seed
```

## 本地开发

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://careforce:careforce@localhost:5432/careforce
alembic upgrade head
uvicorn app.main:app --reload
```

## 测试

测试使用 SQLite + 可控时钟（`FakeClock` 替换 `app.state.clock`），不需要 PostgreSQL：

```bash
pytest
```

覆盖场景：跨周工时拆分与 44h 上限、资格到期、并发接受唯一有效分配、
8 分钟超时重排、重复生成幂等、计划改版只影响未开始任务、人工调整授权与历史。

## 领域规则

### 任务生成
- 护理计划包含：服务时区（IANA）、周期（daily/weekly + interval + byweekday）、
  时间窗口（支持跨夜，如 22:00→02:00）、时长、所需资格、前置任务（前置计划）。
- `POST /plans/{id}/generate` 生成未来 14 天任务；唯一约束
  `(plan_id, plan_version, scheduled_start)` 保证重复调用不重复建单。
- `PUT /plans/{id}` 改版：版本 +1，只取消**尚未开始**（`scheduled_start > now`
  且未分配）的旧版本任务并按新版本重新生成；已开始/已分配/已完成任务不受影响。

### 约束分配（自动与人工共用同一套校验 `validate_assignment`）
1. 资格有效期覆盖**整个**任务时段（`valid_from <= start` 且 `valid_until >= end`）。
2. 与既有班次时间不重叠。
3. 班次间至少休息 10 小时。
4. 周工时 ≤ 44 小时；跨日/跨周班次按实际时间拆分到对应 ISO 周分别累计。
5. 前置任务已完成。

候选人排序：剩余可用时间降序 → 既有负载升序 → ID 升序（稳定）。
无人满足约束时返回 `422` 及每位候选人未满足的具体约束，不强行排班。

### 邀请与超时重排
- 邀请（Offer）8 分钟未接受即失效；`POST /offers/expire` 失效过期邀请并
  自动重新分配（让邀请过期/拒绝的护理员被排除）。
- 接受采用原子更新（`UPDATE ... WHERE status='pending' AND expires_at > now`）
  + 任务状态原子占用 + `assignments.task_id` 唯一约束：多人并发接受、
  超时与接受同时发生时，只保留一个有效分配。
- 重复接受同一邀请幂等返回既有分配，不重复占工时。

### 人工调整
- `POST /tasks/{id}/adjust`：协调员必须被授权该任务所属单元（403），
  走与自动分配完全相同的约束检查（422），记录原因与变更历史
  （`GET /tasks/{id}/history`）。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/plans` | 创建护理计划 |
| GET | `/plans/{id}` | 查看计划 |
| POST | `/plans/{id}/generate` | 生成未来 14 天任务（幂等） |
| PUT | `/plans/{id}` | 计划改版（只影响未开始任务） |
| GET | `/tasks` | 任务列表（status/unit_id/plan_id/caregiver_id 过滤） |
| GET | `/tasks/{id}` | 任务详情 |
| GET | `/tasks/{id}/history` | 变更历史 |
| POST | `/tasks/{id}/offer` | 发邀请（可指定护理员，否则自动选最优） |
| POST | `/tasks/{id}/adjust` | 人工调整（授权 + 同一套约束 + 记录原因） |
| POST | `/tasks/{id}/complete` | 完成任务 |
| POST | `/offers/{id}/accept` | 接受邀请（幂等；并发安全） |
| POST | `/offers/{id}/reject` | 拒绝并立即重新分配 |
| POST | `/offers/expire` | 失效过期邀请并重新分配 |
| POST | `/caregivers` / `/caregivers/{id}/qualifications` | 护理员与资格 |
| POST | `/coordinators` | 协调员（含授权单元） |

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://careforce:careforce@localhost:5432/careforce` | 数据库连接 |
| `OFFER_TTL_MINUTES` | `8` | 邀请有效期 |
| `GENERATION_HORIZON_DAYS` | `14` | 任务生成窗口 |
| `WEEKLY_HOUR_LIMIT` | `44` | 周工时上限 |
| `MIN_REST_HOURS` | `10` | 班次间最小休息 |

## 目录结构

```
app/
  main.py            # FastAPI 入口、异常处理
  config.py          # 环境变量配置
  database.py        # 引擎/会话/Base
  clock.py           # 可替换时钟（测试注入 FakeClock）
  models.py          # SQLAlchemy 模型
  schemas.py         # Pydantic 模型
  services/
    scheduling.py    # 任务生成、计划改版
    assignment.py    # 约束校验、候选人排序、邀请/接受/超时/人工调整
  routers/           # plans / tasks / offers / caregivers / coordinators
  seed.py            # 样例数据
alembic/             # 迁移
tests/               # 可控时钟测试
```
