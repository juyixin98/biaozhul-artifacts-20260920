# SkillPulse 培训执行后端

一个自包含的培训执行服务：主管编写**有序步骤程序**并发布为**不可变版本**；学员报名时**绑定具体版本**，经历席位确认、候补晋升、逐步提交，最终在全部通过后获得**唯一证书**。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Alembic。除数据库容器外不依赖任何外部服务。

## 快速启动（Docker）

```bash
docker compose up --build
```

启动后：

- API 与交互式文档：http://localhost:8000/docs
- OpenAPI：http://localhost:8000/openapi.json
- 健康检查：http://localhost:8000/health

容器启动时会自动执行 `alembic upgrade head`，并在 `SEED_DEMO=1` 时写入演示数据（幂等，可重复启动）。

停止并清空数据卷：

```bash
docker compose down -v
```

### 本地（无 Docker）

```bash
python -m venv .venv && source .venv/bin/pip install -r requirements.txt
cp .env.example .env          # 按需修改 DATABASE_URL
alembic upgrade head
python -m app.seed            # 可选：演示数据
uvicorn app.main:app --reload
```

## 演示数据

种子脚本创建：1 名主管、3 名学员、1 个三步骤程序（已发布 v1，步骤链 1 → 2，1/2 → 3）、1 个未发布 v2 草稿，以及一门容量 2、已占 2 席的课程。启动日志会打印各用户的 `X-User-Id`。

## 身份模型（无外部认证服务）

所有接口通过请求头 `X-User-Id: <用户id>` 识别调用者；服务端仍然强制：

- 学员只能操作**自己的**报名与提交；
- 程序/版本/课程/纠正/时钟接口仅主管可用。

## 核心规则与实现要点

| 需求 | 实现 |
| --- | --- |
| 最多 50 个有序步骤，每步有说明/通过条件/前置 | `steps.order_index 1..50`（CHECK 约束），前置以步骤序号引用 |
| 发布前检查依赖合法且无环 | 创建与发布时双重校验：悬空依赖、自依赖、Kahn 拓扑排序查环 |
| 发布版本不可变，修改产生新版本 | 已发布版本不可再发布、无任何编辑接口；每次修改 = 新版本号 + 内容 SHA-256 |
| 报名绑定具体版本，发布/回滚不影响已开始学习 | `enrollments.version_id` 固定；程序只维护“新报名指向版本”的指针 |
| 容量/截止/候补/幂等报名 | 课程行 `SELECT ... FOR UPDATE` 串行化席位变动；`(learner,version)` 唯一约束保证幂等 |
| 并发抢最后一席不超额 | 行锁 + 容量复核 + `(version_id, seat_number)` 唯一约束三重保障 |
| 48 小时未确认释放，FIFO 晋升 | 席位带 `seat_expires_at`；到期扫描在课程锁内完成过期与晋升 |
| 取消/确认/到期并发一致 | 统一加锁顺序：先课程行、后报名行；状态机集中在一个服务模块 |
| 只能提交自己的步骤、前置未过不能继续 | 所有权校验 + 前置 PASSED 校验 + 报名必须已确认 |
| 重复提交不重复计数 | 提交内容指纹相同则幂等返回；主管纠正后指纹清空允许再交 |
| 主管纠正必须写原因 | `reason` 必填；纠正后自动重算完成状态与证书有效性 |
| 全部通过只发一张证书 | 每报名仅一行证书，序列号由 (报名,版本) 确定；失效/恢复在同一行翻转 |

### 可控时钟

生产代码从不直接读取系统时间，统一走 `app/clock.py`（读 `clock_overrides` 单行表）。主管可通过 `/admin/clock` 设定、推进、重置虚拟时钟，因此 48 小时到期逻辑无需真实等待即可测试。

## API 摘要

完整说明见 [docs/API.md](docs/API.md)，交互式文档见 `/docs`。

- 用户：`POST /users`、`GET /users/me`
- 程序/版本：`POST /programs`、`POST /programs/{id}/versions`、`POST /versions/{id}/publish`、`PUT /programs/{id}/current-version`
- 课程/报名：`POST /courses`、`POST /courses/{id}/enroll`、`POST /enrollments/{id}/confirm|cancel`、`POST /courses/{id}/expire-holds`
- 学习：`POST /enrollments/{id}/steps/{order}/submissions|corrections`、`GET /enrollments/{id}/progress|certificate`
- 时钟：`GET/PUT /admin/clock`、`POST /admin/clock/advance|reset`

## 测试

```bash
# 需要一个可访问的 PostgreSQL（默认连接见 tests/conftest.py）
createdb skillpulse_test    # 或用现有超级用户创建
.venv/bin/python -m pytest -q
```

测试覆盖：版本隔离、容量并发竞争（8 线程真实并发抢 1 席）、候补 FIFO 晋升与压缩、48 小时到期（虚拟时钟）、依赖图校验（悬空/自依赖/环/50 步上限）、提交门禁与幂等、证书签发唯一性与纠正失效/恢复。

## 项目结构

```
app/
  models.py            # SQLAlchemy 模型与约束
  schemas.py           # Pydantic 输入输出
  clock.py             # 可控时钟
  services/
    graph.py           # 依赖校验与内容哈希
    programs.py        # 程序/版本/发布/回滚
    enrollments.py     # 报名、席位锁、候补、到期
    progress.py        # 提交、纠正、完成状态、证书
  routers/             # HTTP 接口
  seed.py              # 演示数据
alembic/               # 迁移（initial schema）
tests/                 # pytest 测试
Dockerfile docker-compose.yml entrypoint.sh
```
