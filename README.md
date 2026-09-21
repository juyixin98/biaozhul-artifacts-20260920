# CareForce — 护理任务排班后端

任务生成、约束分配与超时重排。**不包含**薪资、志愿者管理或医疗决策。

技术栈：FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic · Docker Compose。

## 业务规则

### 任务生成
- 护理计划包含：服务时区、周期（每日 / 每周指定星期）、服务时间窗口、时长、
  所需资格、前置任务。
- 一次生成未来 **14 天**任务（窗口起始时刻开始，时长为计划时长）。
- 以 `(plan_id, occurrence_key=服务当地日期)` 为幂等键，**重复生成不会重复建单**。
- 计划改版自增 `version`：只影响尚未开始的任务——
  - `pending` 任务直接更新到新版本时间；
  - 已发出但未接受的 `invited` 邀请立即失效并按新版本重建；
  - `assigned` / `completed` 任务保持旧版本、旧时间不动；
  - 改版后不再出现的日期（如每日改每周），未开始任务被取消。
- 前置任务：同一服务对象、同一天的前置计划任务完成后，后续任务才能完成。

### 分配约束（自动分配、人工调整、接受时复核走同一套检查）
- 人员属于任务所在授权单元且处于在职状态；
- **资格必须覆盖整个任务时段**：`valid_from <= 任务开始` 且
  `valid_until >= 任务结束`（任务中途到期不算合格）；
- 与该人员其他有效班次（invited/assigned）时间不重叠；
- 相邻两班之间至少休息 **10 小时**；
- 每 ISO 周累计工时不超过 **44 小时**，按人员家庭时区计算；
  **跨当地午夜的班次按实际时间拆分到两个 ISO 周**分别计工时。
- 候选人排序：可行者优先 → 任务所触及最紧周的剩余可用时间多者优先 →
  既有负载少者优先 → **ID 升序稳定排序**。
- 没有合适人员时**不强行排班**，返回最近候选人未满足的具体约束
  （HTTP 409，body 内含每条违规的 code/明细）。

### 邀请与超时
- 邀请 **8 分钟**内未接受即失效（`POST /admin/reap-expired`）。
- 失效后自动重排给下一位可行人员，**立即重排排除刚放鸽子的人员**。
- 并发接受只有一个生效：任务行 `SELECT ... FOR UPDATE` +
  `assignments` 上的部分唯一索引（每任务至多一条 invited/assigned）。
- 接受时再做一次全部约束复核；与其他新订班次或超时重排竞争时，
  先提交者胜，负方观察到新状态后退出。
- 接受支持幂等键 `request_key`；重复请求重放同一结果，**不会重复占工时**。

### 协调员授权与人工调整
- 协调员只能调度被授权单元的计划/任务，越权返回 403。
- 人工指定、改期、取消**都经过同一套约束检查**，违规即拒绝（409），
  必须填写原因；所有动作写入 `assignment_events` 变更历史
  （`GET /tasks/{id}/events` 可查）。

## 快速开始（Docker）

```bash
docker compose up --build
# API:        http://localhost:47823  (容器内 8000；本机 8000 常被占用)
# Swagger UI: http://localhost:47823/docs
# PostgreSQL: localhost:5434 (careforce/careforce)
```

容器启动会自动执行迁移并写入样例数据（两个单元、两个协调员、
3 名护理员、两个带前置关系的每日计划、28 个任务）。

## 本地开发（已有 PostgreSQL）

```bash
pip install -r requirements.txt
cp .env.example .env            # 按需修改 CAREFORCE_DATABASE_URL
createdb careforce              # 或: sudo -u postgres createdb careforce
alembic upgrade head
python -m app.seed              # 可选：样例数据
uvicorn app.main:app --reload
```

运行测试（需要 `careforce_test` 数据库；测试使用可控时钟，冻结在
2026-09-21 周一 00:00 UTC）：

```bash
createdb careforce_test
pytest
```

## 身份与鉴权（演示约定）

演示用请求头传递身份（生产环境应替换为已验证的 JWT claim）：

- 协调员：`X-Coordinator-ID: <id>`
- 护理员：`X-Worker-ID: <id>`

## API 概览

交互文档：启动后访问 `/docs`（Swagger）或 `/redoc`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/directory/units` / `/coordinators` / `/workers` | 建立目录数据 |
| POST | `/directory/workers/{id}/qualifications` | 录入资格及有效期 |
| GET | `/directory/coordinators/me` | 查询自己的授权单元 |
| GET | `/directory/workers` | 护理员列表（可按 unit_id 过滤） |
| POST | `/plans` | 建计划（可带 `prerequisite_plan_ids`） |
| PATCH | `/plans/{id}` | 改版（只影响未开始任务） |
| POST | `/plans/{id}/generate` | 生成/刷新未来 14 天任务（幂等） |
| GET | `/tasks` | 任务查询（plan_id / unit_id / status） |
| POST | `/tasks/{id}/allocate` | 自动分配；无人合适返回 409 + 具体约束 |
| POST | `/tasks/{id}/accept` | 护理员接受（body 可带 `request_key` 幂等） |
| POST | `/tasks/{id}/decline` | 婉拒邀请 |
| POST | `/admin/reap-expired` | 失效超时邀请并自动重排 |
| GET | `/workers/me/invitations` | 我收到的有效邀请 |
| POST | `/tasks/{id}/manual-assign` | 人工指定（同样过约束，需 reason） |
| POST | `/tasks/{id}/reschedule` | 同日改期并重排（需 reason） |
| POST | `/tasks/{id}/cancel` | 取消任务（需 reason） |
| POST | `/tasks/{id}/complete` | 完成任务（前置任务须先完成） |
| GET | `/tasks/{id}/events` | 该任务的完整变更历史 |

### 典型流程

```bash
# 1) 建数据（首次可用 seed 跳过）
UNIT=...; COORD=...
curl -X POST localhost:8000/plans -H "X-Coordinator-ID: $COORD" \
  -H 'Content-Type: application/json' \
  -d '{"care_recipient_id":"r1","unit_id":'$UNIT',"recurrence":"daily",
       "window_start":"08:00","window_end":"10:00","duration_minutes":60,
       "required_qualifications":["personal_care"]}'

# 2) 生成 14 天任务
curl -X POST localhost:8000/plans/1/generate -H "X-Coordinator-ID: $COORD"

# 3) 分配（失败时看 409 里的 violations）
curl -X POST localhost:8000/tasks/1/allocate -H "X-Coordinator-ID: $COORD"

# 4) 护理员 8 分钟内接受（带幂等键）
curl -X POST localhost:8000/tasks/1/accept -H "X-Worker-ID: 1" \
  -H 'Content-Type: application/json' -d '{"request_key":"accept-0001"}'

# 5) 超时重排（可由定时任务周期调用）
curl -X POST localhost:8000/admin/reap-expired -H "X-Coordinator-ID: $COORD"
```

## 测试覆盖

`pytest`（45 个用例，真实 PostgreSQL + 真实线程并发）：

- **跨周工时**：跨当地午夜班次拆分到两个 ISO 周；44h 上限边界；
  invited 未接受也预占工时；
- **资格到期**：资格在任务中途到期不合格；覆盖整个时段才合格；
- **并发接受**：两个线程同时接受同一邀请，仅一个生效、仅一行 assigned；
  同一幂等键重复请求不重复占工时；
- **超时重排**：8:01 失效、自动给下一人、排除上一人、未到期不动；
- **重复生成**：重复调用 created=0；改版只动未开始任务、未接受邀请失效；
- **授权与人工调整**：越权 403、人工违规被拒并记录原因、前置任务顺序、
  以及完整 HTTP 端到端流程。

## 数据模型要点

- `care_plans.version` + `tasks.plan_version`：版本快照。
- `tasks` 唯一约束 `(plan_id, occurrence_key)`：幂等生成。
- `assignments` 部分唯一索引：`(task_id) WHERE status IN ('invited','assigned')`
  ——数据库层面保证每任务至多一个有效分配。
- `assignment_events`：不可变审计流水（创建/接受/婉拒/失效/人工调整/取消）。
- `idempotent_requests`：接受请求幂等键 → 分配结果。
