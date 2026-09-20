# SkillPulse — 培训执行后端

一个自包含的培训执行系统：主管编写**有序、带前置依赖**的培训程序并发布不可变版本；学员报名**绑定具体版本**，在容量限制下抢位、候补；席位 48 小时未确认自动释放并按序晋升；学员按前置门控提交步骤结果，主管可纠正（必须写原因）；全部步骤通过后只签发**一张绑定版本的证书**，纠正会同步撤销/恢复证书。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic · Docker Compose。除一个 PostgreSQL 容器外**不依赖任何外部服务**。

---

## 1. 快速开始（Docker）

```bash
docker compose up --build
```

启动时容器会自动：等待数据库 → `alembic upgrade head` → 写入演示数据（`SEED_DEMO=1`）。

- API： http://localhost:8000
- 交互式文档（Swagger）： http://localhost:8000/docs
- 健康检查： `GET /health`

演示账号（固定 token，可直接使用）：

| 角色 | 登录名 | 密码 | Bearer Token |
|---|---|---|---|
| 主管 | `sup` | `sup-password` | `demo-token-sup` |
| 学员 | `alice` | `alice-password` | `demo-token-alice` |
| 学员 | `bob` / `carol` / `dave` | `<name>-password` | `demo-token-<name>` |

演示程序“Workplace Safety Certification”（容量 3，4 个有依赖的步骤）：alice 已全部通过并持有有效证书；bob 已确认且通过第 1 步；carol 持有未确认席位；dave 在候补队列。

调用示例：

```bash
curl -s http://localhost:8000/health
curl -s http://localhost:8000/api/my/enrollments \
  -H "Authorization: Bearer demo-token-alice"
```

停止并清空数据：

```bash
docker compose down -v
```

### 不使用 Docker（本地运行）

```bash
python3 -m venv .venv && source .venv/bin
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://USER:PASS@localhost:5432/skillpulse
alembic upgrade head
python seed_demo.py            # 可选
uvicorn app.main:app --reload
```

---

## 2. 数据库迁移

```bash
alembic upgrade head     # 应用迁移
alembic downgrade base   # 回滚全部
alembic revision --autogenerate -m "change"   # 模型变更后生成新迁移
```

初始迁移 `alembic/versions/0001_initial.py` 手工编写，与 ORM 模型严格一致，包含关键约束：

- `uq_version_number`：同一程序版本号唯一；
- `uq_step_position` / `uq_step_prereq` / `ck_prereq_not_self`：步骤顺序、依赖唯一、禁止自依赖；
- **部分唯一索引** `uq_enrolment_active_learner ON (program_id, learner_id) WHERE status IN ('pending','confirmed','waitlisted')`：从数据库层面保证报名幂等；
- `uq_result_enrolment_step`：每条报名每个步骤至多一条结果；
- `uq_certificate_enrolment` 与 `uq_certificate_serial`：每条报名至多一张证书、序列号全局唯一。

---

## 3. API 说明

除 `/api/auth/register`、`/api/auth/login`、`/health` 外，所有接口都需要请求头：

```
Authorization: Bearer <token>
```

错误响应统一为 `{"detail": "..."}`，状态码：401 未认证 / 403 越权 / 404 不存在 / 409 状态冲突（如容量、截止时间、不可变、前置未满足等）/ 422 校验失败（如步骤数、依赖环）。

### 3.1 认证

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/register` | 注册并直接返回 token。body: `name, login, password(≥6), role(supervisor|learner)` |
| POST | `/api/auth/login` | 登录换取 token |
| GET | `/api/auth/me` | 当前用户 |

### 3.2 程序与版本（主管）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/programs` | 创建程序（`title, capacity≥0, enrollment_deadline`），自动生成草稿版本 1 |
| GET | `/api/programs/{id}` | 程序详情（含 `current_version_id`） |
| GET | `/api/programs/{id}/versions` | 列出全部版本（含草稿）和步骤 |
| GET | `/api/programs/versions/{version_id}` | 单个版本（含步骤与前置位置） |
| POST | `/api/programs/{id}/drafts` | 基于当前发布版（或 `base_version_id`）创建新草稿；同时只允许一个草稿 |
| PUT | `/api/programs/versions/{version_id}/steps` | **整体替换**草稿的有序步骤（1–50 步） |
| POST | `/api/programs/versions/{version_id}/publish` | 校验依赖（未知引用/自依赖/环）并冻结发布 |
| POST | `/api/programs/{id}/rollback/{version_id}` | 把“新报名所见版本”切回旧发布版 |

步骤对象：

```json
{
  "title": "Hands-on drill",
  "instruction": "Perform the drill under observation",
  "pass_condition": "Observer signs off",
  "prerequisite_positions": [1, 2]
}
```

`prerequisite_positions` 使用 **1 基位置**引用同一版本中的步骤。

**版本规则**

- 已发布版本不可修改（步骤、状态都冻结），任何修改必须创建新草稿 → 发布为新版本；
- 发布与替换步骤时都会校验：步骤数 1–50、前置必须存在、禁止自依赖、依赖图必须无环（任意长度的环都会被拒绝并给出环路）；
- 回滚只改变 `current_version_id`，**已经开始的学员仍停留在报名时绑定的版本**。

### 3.3 报名与席位（学员）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/programs/{id}/enroll` | 报名（容量未满 → `pending` 持座；满员 → `waitlisted`）。重复报名幂等，返回同一条 |
| GET | `/api/enrollments/{id}` | 报名详情（仅本人或主管可看） |
| GET | `/api/my/enrollments` | 我的全部报名 |
| POST | `/api/enrollments/{id}/confirm` | 在 48 小时内确认席位（`pending`→`confirmed`） |
| POST | `/api/enrollments/{id}/cancel` | 取消；持座者取消会立即按 FIFO 晋升候补 |

席位规则：

- 报名时绑定当时的 `current_version_id`，之后发布新版本或回滚都不影响学习内容；
- 超过 `enrollment_deadline` 不能报名；候补晋升不受截止时间影响（报名时已排队）；
- **席位 48 小时未确认即释放**（`pending`→`expired`），随后按候补位置从小到大晋升，被晋升者获得**自己全新的 48 小时倒计时**；
- 到期既会在 confirm/cancel/enroll 时惰性处理，也可由主管主动触发：
  `POST /api/admin/expire-seats?program_id={可选}`，返回 `{"expired": n, "promoted": m}`；
- 取消/到期后可以重新报名，生成一条新的报名记录（旧记录保留为历史）。

### 3.4 步骤结果（学员提交 / 主管评定）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/enrollments/{id}/steps/{step_id}/submit` | 学员提交（body `{"content": "..."}`） |
| GET | `/api/enrollments/{id}/results` | 该报名的全部结果（本人或主管） |
| PUT | `/api/enrollments/{id}/steps/{step_id}/evaluation` | 主管评定：`{"status":"passed|failed","reason":"..."}` |
| PATCH | `/api/enrollments/{id}/steps/{step_id}/correct` | 主管纠正已评定结果，**reason 必填非空** |
| GET | `/api/enrollments/{id}/certificate` | 查看证书（无证书时 404） |

进度规则：

- 只有报名**本人**能提交，且必须是 `confirmed` 状态；
- 前置步骤全部 `passed` 才开放当前步骤；前置后来被改判 failed，后续步骤立即重新封闭；
- 每个（报名, 步骤）只有一条结果：重复提交返回原记录，不覆盖、不重复计数；
- 主管每次纠正都会写入审计表 `result_corrections`（原状态、新状态、原因、时间）。

### 3.5 证书

全部步骤 `passed` 时，系统在同一事务内为该报名签发唯一证书：

```json
{
  "id": 1,
  "enrollment_id": 1,
  "version_id": 1,
  "serial_number": "SP-1-00000001",
  "content_digest": "<64 hex chars>",
  "revoked": false,
  "issued_at": "...",
  "revoked_at": null
}
```

- 序列号格式 `SP-{version_id}-{enrollment_id:08d}`，**确定性生成**，重试不会产生第二张；
- `content_digest` 是对“序列号/版本/学员/各步骤状态与提交时间”规范拼接后的 SHA-256 十六进制摘要（64 字符）；
- 纠正导致不再全部通过时，同一张证书被标记 `revoked=true`（记录 `revoked_at`）；纠正恢复通过后**复用同一行、同一序列号**恢复有效。任意时刻，每个报名最多一张有效证书。

---

## 4. 关键并发设计

- 所有会改变席位的操作（报名/确认/取消/到期清理）都先对 `programs` 行执行 `SELECT … FOR UPDATE`，把容量计数、到期释放、候补晋升串行化到单个程序上；
- 候补晋升使用 `FOR UPDATE SKIP LOCKED` 取队首，避免并发清理互相阻塞；
- 数据库部分唯一索引兜底并发重复报名（即使两个请求同时通过应用层检查）；
- 到期处理与候补晋升在一个事务内提交后，再返回/抛出业务错误，保证“席位释放 + 晋升 + 报名状态”始终一致。

---

## 5. 可控时钟与测试

业务代码只从 `app/clock.py` 读取当前时间。测试通过 `set_clock()` / `reset_clock()` 固定和推进时间，从而确定性地验证 48 小时到期，无需真实等待。

### 运行测试

测试使用**真实 PostgreSQL**（行锁与部分索引是 PostgreSQL 特性）：

```bash
# 默认连接 postgresql+psycopg://skillpulse:skillpulse@localhost:5432/skillpulse_test
# 可用 TEST_DATABASE_URL 覆盖
pytest -q
```

27 个测试，按缓存中的契约组织为 5 个文件：

| 文件 | 覆盖 |
|---|---|
| `tests/test_versions.py` | 发布不可变、步骤数上限、未知/自前置拒绝、短环与长环拒绝、报名版本隔离与回滚隔离 |
| `tests/test_enrollment.py` | 容量与候补顺序、重复报名幂等、截止时间、取消（未确认/已确认）触发晋升、越权防护、取消后重报 |
| `tests/test_capacity_race.py` | **12 线程真实并发**抢 3 席不超额；同一学员并发重复报名只产生一条记录（多 session + 行锁） |
| `tests/test_expiry.py` | 48h 边界释放、到期不能确认、已确认不过期、晋升席位使用独立时钟的连环晋升 |
| `tests/test_progress.py` | 确认才可提交、只能提交自己的、前置门控（含改判重封闭）、重复提交幂等、证书一次性/重试安全、纠正必须有原因、纠正失败撤销证书、改回通过恢复同一证书 |

---

## 6. 项目结构

```
app/
  main.py                # FastAPI 应用与统一异常处理
  config.py clock.py     # 环境配置 / 可控时钟
  database.py models.py  # 引擎会话 / ORM 模型与约束
  security.py errors.py  # PBKDF2 密码哈希、token / 领域异常
  deps.py                # 认证与角色依赖
  schemas/dto.py         # 请求/响应模型
  services/              # 业务逻辑（版本、报名、进度证书、认证）
  routers/               # HTTP 路由
alembic/                 # 迁移环境与初始迁移
tests/                   # pytest 测试套件
seed_demo.py             # 幂等演示数据
docker/entrypoint.sh     # 等待DB + 迁移 + 种子 + 启动
Dockerfile docker-compose.yml
```
