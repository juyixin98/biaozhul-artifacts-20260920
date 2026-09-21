# SkillPulse 培训执行后端

基于 FastAPI + SQLAlchemy + PostgreSQL + Alembic 的培训执行系统：版本化培训程序、
容量受限的课程报名（候补队列 + 48 小时席位保留）、前置条件约束的步骤结果提交，
以及幂等的证书签发与吊销。

## 快速启动（Docker）

```bash
docker compose up --build
```

- API: http://localhost:8000 （交互式文档 http://localhost:8000/docs）
- 启动时自动执行 `alembic upgrade head`，并（`SEED_DEMO=true` 时）写入演示数据：
  一个已发布的 3 步安全培训程序、容量为 2 的课程、1 名已确认学员、
  1 名待确认学员、1 名候补学员。
- PostgreSQL 暴露在宿主机 `15432` 端口（容器内仍为 5432）。

运行测试（容器内，独立的 `skillpulse_test` 数据库）：

```bash
docker compose --profile test run --rm test
```

本地运行（需可访问的 PostgreSQL）：

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/skillpulse
.venv/bin/alembic upgrade head
.venv/bin/uvicorn app.main:app --reload
.venv/bin/pytest            # 测试使用 DATABASE_URL 指向的实例上的独立测试库
```

## 认证模型

每个请求通过请求头声明身份（演示用，无密码）：

- `X-User-Id`: 用户标识（必填）
- `X-User-Role`: `learner`（默认）或 `supervisor`

学员只能操作自己的报名与结果；程序编写、发布、回滚、纠正、到期任务需要
`supervisor` 角色。

## 核心规则

- **程序版本**：主管编写 1–50 个有序步骤，每步含说明、通过条件、前置步骤。
  创建/发布时校验：步骤数上限、键唯一、前置步骤存在、依赖无环（Kahn 拓扑检测）。
  发布后不可变，任何修改必须新建版本；回滚仅改变"当前版本"指针。
- **报名**：报名时绑定具体版本（默认当前发布版），之后发布/回滚不影响在途学员。
  课程有容量与报名截止；重复报名幂等（唯一约束 + 行锁，返回原记录）。
  席位计数在课程行 `SELECT ... FOR UPDATE` 下更新，并发抢最后一个席位不会超额。
- **席位保留**：报名成功进入 `pending`，48 小时内须 `confirm`，否则到期任务
  （`POST /jobs/expire-seats`）释放席位并按报名先后晋升候补；取消/确认/到期
  在同一课程行锁下串行，席位计数与报名状态始终一致。
- **步骤结果**：仅本人可提交；座位确认后才能提交；前置步骤未通过则 409；
  重复提交幂等（返回原记录，HTTP 200，不重复计数）。主管纠正必须填写原因，
  完成状态与证书有效性在同一事务内同步。
- **证书**：全部步骤通过后签发且仅签发一张（`enrollment_id` 唯一约束 +
  savepoint 防并发重复），含唯一序列号与内容 SHA-256 摘要；重试不重复发证。
  纠正导致不满足条件时吊销，恢复满足时同一张证书重新生效。
- **可控时钟**：所有业务时间经 `app.clock.now()` 获取，测试用
  `clock.set_clock()` 注入假时钟验证 48 小时到期逻辑（见 `tests/conftest.py`
  的 `fake_clock` fixture）。

## API 一览

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/programs` | supervisor | 创建程序 + 草稿 v1（含步骤） |
| GET | `/programs/{id}` | 任意 | 程序详情（含当前版本指针） |
| GET | `/programs/{id}/versions` | 任意 | 版本列表 |
| POST | `/programs/{id}/versions` | supervisor | 基于新步骤集创建草稿版本 |
| PUT | `/versions/{vid}/steps` | supervisor | 替换草稿步骤（已发布则 409） |
| POST | `/versions/{vid}/publish` | supervisor | 校验并发布（幂等） |
| GET | `/versions/{vid}` | 任意 | 版本详情（含步骤） |
| POST | `/programs/{id}/rollback` | supervisor | 当前版本回滚到已发布的旧版本 |
| POST | `/courses` | supervisor | 开班（容量、报名截止） |
| GET | `/courses/{id}` | 任意 | 课程详情（含已占席位） |
| POST | `/courses/{id}/enrollments` | 本人/supervisor | 报名（幂等；满员进候补） |
| GET | `/enrollments/{id}` | 本人/supervisor | 报名详情 |
| POST | `/enrollments/{id}/confirm` | 本人 | 48h 内确认席位（幂等） |
| POST | `/enrollments/{id}/cancel` | 本人/supervisor | 取消并触发候补晋升 |
| GET | `/enrollments/{id}/progress` | 本人/supervisor | 逐步骤进度 |
| POST | `/enrollments/{id}/results` | 本人 | 提交步骤结果（幂等） |
| POST | `/results/{id}/corrections` | supervisor | 纠正结果（原因必填） |
| GET | `/enrollments/{id}/certificate` | 本人/supervisor | 查看证书 |
| GET | `/certificates/{serial}` | 任意 | 按序列号公开验证证书 |
| POST | `/jobs/expire-seats` | supervisor | 处理到期的 48h 席位保留 |

## 典型流程

```bash
SUP='-H "X-User-Id: sup-1" -H "X-User-Role: supervisor"'
# 1. 主管建程序（草稿）→ 发布
curl -sX POST localhost:8000/programs $SUP -H 'Content-Type: application/json' -d '{
  "title":"入职培训","description":"","change_note":"v1",
  "steps":[{"step_key":"intro","title":"入门","pass_condition":"阅读完成"},
           {"step_key":"exam","title":"测验","pass_condition":">=80","prerequisites":["intro"]}]}'
curl -sX POST localhost:8000/versions/<version_id>/publish $SUP
# 2. 开班 → 学员报名 → 确认 → 逐步提交结果 → 自动发证
curl -sX POST localhost:8000/courses $SUP -H 'Content-Type: application/json' -d '{
  "program_id":"<pid>","title":"九月班","capacity":30,
  "enrollment_deadline":"2026-10-01T00:00:00Z"}'
curl -sX POST localhost:8000/courses/<cid>/enrollments -H 'X-User-Id: alice' -d '{"learner_id":"alice"}'
curl -sX POST localhost:8000/enrollments/<eid>/confirm -H 'X-User-Id: alice'
curl -sX POST localhost:8000/enrollments/<eid>/results -H 'X-User-Id: alice' -d '{"step_key":"intro","passed":true}'
curl -s  localhost:8000/enrollments/<eid>/certificate -H 'X-User-Id: alice'
```

## 测试覆盖

`tests/` 共 25 个用例，覆盖：版本隔离（发布/回滚不影响在途学员）、
依赖校验（环/未知前置/超 50 步/重复键）、容量并发竞争（12 线程抢 1 席）、
取消与到期并发下席位一致性、候补按序晋升、48h 到期（假时钟）、
报名幂等、前置步骤门禁、重复提交不重复计数、纠正必填原因、
证书单次签发/吊销/恢复与公开验证。

## 目录结构

```
app/
  main.py            FastAPI 装配与异常映射
  config.py          环境配置（DATABASE_URL、席位保留时长等）
  clock.py           可控时钟
  models.py          SQLAlchemy 模型
  schemas.py         Pydantic 请求/响应模型
  security.py        请求头身份与角色
  services/          业务规则（程序版本、报名、进度、证书）
  routers/           HTTP 路由
  seed.py            幂等演示数据
alembic/             迁移（0001 initial schema）
tests/               pytest 测试（真实 PostgreSQL）
Dockerfile / docker-compose.yml
```
