# SkillPulse — 培训执行后端

主管编写**版本化、不可变**的培训程序（最多 50 个有序步骤），学员报名**绑定具体版本**，
系统处理容量、报名截止、48 小时席位确认与候补晋升、有序步骤评审、纠正审计，以及
**全条件满足后只签发一张证书**。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2.0 · PostgreSQL 16 · Alembic。
除 PostgreSQL（容器化）外不依赖任何外部服务。

---

## 1. 快速启动

### Docker Compose（推荐，自带 PostgreSQL）

```bash
docker compose up --build
# API:        http://localhost:8099
# Swagger UI: http://localhost:8099/docs
# Postgres:   localhost:55432 (skillpulse/skillpulse)
```

启动时容器会自动：等待数据库 → `alembic upgrade head` → 幂等写入演示数据 → 启动 uvicorn。
后台席位清扫任务默认开启（`ENABLE_SWEEPER=true`，每 30 秒一次）。

### 本地运行（连接已有 PostgreSQL）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 建库
sudo -u postgres psql -c "CREATE ROLE skillpulse LOGIN PASSWORD 'skillpulse';"
sudo -u postgres psql -c "CREATE DATABASE skillpulse OWNER skillpulse;"

alembic upgrade head
python -m app.seed                      # 可选：演示数据（可重复执行）
uvicorn app.main:app --reload --port 8000
```

配置通过环境变量（或 `.env`）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+psycopg2://skillpulse:skillpulse@localhost:5432/skillpulse` | 数据库连接 |
| `ENABLE_SWEEPER` | `true` | 后台 48h 席位清扫开关；测试关闭，改由 API 驱动 |
| `SWEEPER_INTERVAL_SECONDS` | `30` | 清扫周期 |
| `SEAT_CONFIRM_HOURS` | `48` | 席位确认时限 |

### 演示数据（`python -m app.seed`，幂等）

* 用户：主管 **Alice**（id 1）；学员 **Bob/Carol/Dan**（id 2/3/4）。
* 程序 *New Trainer Onboarding*：**v1（4 步）已发布**，Bob 已在 v1 完成并持有一张
  **valid 证书**；随后发布 **v2（5 步）** 并成为当前版本——演示"发布新版本不改变
  已开始的学习内容"。
* v2 课程容量 2：Carol 已确认、Dan 已获得席位（48h 未确认会被释放）。

---

## 2. 鉴权

演示后端用请求头 `X-User-Id: <用户id>` 标识调用者（边缘可信、无外部 IdP）。
角色来自数据库：`supervisor` / `learner`，每个接口按角色鉴权。

---

## 3. API 说明

交互式文档见 `/docs`（Swagger）。错误统一返回：

```json
{ "error": { "code": "validation_error", "message": "..." } }
```

### 用户（演示用开户接口）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/users` | `{name, role}` 创建主管/学员 |
| GET | `/users` | 用户列表 |

### 程序与版本（主管）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/programs` | `{title, description}` |
| GET | `/programs` · `/programs/{id}` | 列表/详情（含所有版本） |
| POST | `/programs/{id}/versions` | **创建新版本**：`{steps:[...], publish:true}`，1–50 步 |
| POST | `/programs/versions/{id}/publish` | 发布草稿版本（已发布则 409，不可变） |
| GET | `/programs/versions/{id}` | 版本详情（含步骤、`content_digest`） |
| POST | `/programs/{id}/rollback` | `{version_id}` 将当前指向切回某个**已发布**旧版本 |

步骤对象：

```json
{
  "key": "safety",
  "position": 2,
  "instruction": "完成安全走查",
  "pass_condition": "测验 >= 90 分",
  "prerequisite_keys": ["intro"]
}
```

发布前校验（失败返回 422）：

* 步骤数 1–50；`position` 必须是从 1 开始的连续整数；`key` 在版本内唯一；
* 前置必须存在、不能指向自己、必须是**更靠前**的步骤；
* 依赖图必须**无环**（Kahn 拓扑排序校验，菱形等合法 DAG 允许）；
* 发布时计算 **SHA-256 内容摘要**（步骤结构规范化后哈希）。
* **已发布版本任何内容不可修改**；任何修改都必须 `POST .../versions` 产生新版本。
  课程与报名引用的是版本 id，回滚/新发版不影响在途学习。

### 课程与报名

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/courses` | 主管 | `{title, version_id, capacity, enroll_deadline}`，版本必须已发布 |
| GET | `/courses` · `/courses/{id}` | 任意 | 含 `seats_taken/seats_available/waitlist_count` |
| POST | `/courses/{id}/enroll` | 学员 | 报名（幂等）；有余位→`pending_confirmation`，否则 `waitlisted` |
| POST | `/courses/{id}/confirm` | 学员 | 48h 内确认席位 → `confirmed`（幂等） |
| POST | `/courses/{id}/cancel` | 学员 | 取消（幂等），立即按 FIFO 晋升候补 |
| GET | `/courses/{id}/my-enrollment` | 学员 | 本人报名；候补时额外返回 `queue_position` |

席位状态机：

```
waitlisted ──(有空位,按序)──▶ pending_confirmation ──confirm──▶ confirmed
     ▲                              │  │
     │ (48h未确认→过期;或主动取消)    │  └─cancel──▶ cancelled
     └──────────────────────────────┘
pending_confirmation ──超过48h未确认──▶ expired（同时晋升下一位候补）
cancelled/expired 可重新报名（复用同一行，同一人在同一课程只有一行）
```

规则：

* 报名时把 `version_id` **固化到报名行**；之后程序发布新版或回滚都不改变它。
* 超过 `enroll_deadline` 报名 → 409。
* 重复报名：已有 `waitlisted/pending/confirmed` 时原样返回（幂等），不产生新行、
  不占两个席位。
* **并发抢最后席位不会超额**：所有席位决策先对 `courses` 行 `SELECT ... FOR UPDATE`，
  在同一事务内统计占用并分配。
* 候补为 FIFO（`waitlist_position` 紧凑 1..N，晋升后重排）。

### 学习进度

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/enrollments/{id}/steps/{key}/submit` | 学员本人 | `{content}` 提交步骤结果 |
| POST | `/enrollments/{id}/steps/{key}/review` | 主管 | `{passed}` 评审最新提交 |
| POST | `/enrollments/{id}/steps/{key}/correct` | 主管 | **纠正**：`{new_status, reason}`，reason 必填 |
| GET | `/enrollments/{id}/results` | 已登录 | 该报名各步骤结果 |
| GET | `/enrollments/{id}/certificate` | 本人/主管 | 证书；未签发 404 |

规则：

* 学员只能提交**自己**报名的步骤（否则 403）；报名必须已 `confirmed`。
* **前置步骤未通过不能继续提交**（409）；前置是该报名所绑定版本内声明的依赖。
* 重复提交：每个 (报名, 步骤) 只有一行，`attempts` 累加、内容更新，**不重复计数**。
* 主管纠正必须写 `reason`（写入 `result_corrections` 审计表）。把某步骤纠正为
  `failed` 时，**级联撤销所有（传递）依赖它的下游通过状态**；纠正后同步重算完成态
  与证书有效性，全部在同一事务内。

### 证书

* 只有当绑定版本的**全部步骤都为 passed**才完成报名并签发证书。
* 每个报名**只有一张证书**（DB 唯一约束 + 保存点处理并发），具有：
  * 全局唯一流水号 `SKP-YYYYMMDD-NNNNNN`；
  * 绑定版本的 **内容摘要 `content_digest`**（与版本发布时摘要一致）。
* 重试/并发完成不会重复发证。纠正导致不再全部通过时，**同一证书行**置为
  `invalid`（流水号保留、不删不换）；恢复全部通过后同一行回到 `valid`。

### 维护接口（主管；可控时钟）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/admin/clock` | 当前应用时间、与真实时间偏移 |
| POST | `/admin/clock/freeze` | `{at}` 冻结到指定时刻 |
| POST | `/admin/clock/advance` | `{seconds}` 推进时钟 |
| POST | `/admin/clock/reset` | 恢复真实时间 |
| POST | `/admin/sweep-seat-expiries?course_id=` | 立即扫描，过期未确认席位并晋升候补 |

测试中关闭后台清扫（`ENABLE_SWEEPER=false`），用这些接口**确定性地**验证 48h 到期逻辑。

---

## 4. 测试

覆盖需求中的所有关键场景（真实 PostgreSQL + 真实 OS 线程）：

```bash
# 需要测试库：
sudo -u postgres psql -c "CREATE DATABASE skillpulse_app_test OWNER skillpulse;"

ENABLE_SWEEPER=false pytest -q
```

| 测试文件 | 覆盖点 |
|---|---|
| `test_program_versioning.py` | 50 步上限、未知/自指/前向前置、无环（拓扑）、菱形 DAG、发布不可变、改动必须新版本、回滚、草稿不可开课 |
| `test_version_isolation.py` | 报名绑定版本；新版发布/回滚不改在途内容；证书摘要跟随所绑版本 |
| `test_capacity_race.py` | 8 线程抢 3 席位不超额、候补 FIFO、重复报名幂等、0 容量、截止时间 |
| `test_waitlist_expiry.py` | 可控时钟：47h 不过期、48h 过期并**同事务晋升**、48h 内确认保留、过期后确认被拒、过期重新报名复用行 |
| `test_seat_concurrency.py` | 确认/取消/多个清扫并发交叉，最终席位与状态严格一致、无超卖、无重复晋升 |
| `test_progress_certificates.py` | 本人鉴权、前置未通过拦截、重复提交单行、纠正需原因、级联撤销下游、完成态/证书联动、一报名一证书、恢复后复用同一证书 |
| `test_certificate_concurrency.py` | 4 线程并发触发完成发证，最终仅一张证书 |

---

## 5. 数据模型与并发设计要点

* **版本不可变**：`program_versions.status='published'` 后不再有更新路径；
  `programs.current_version_id` 只决定"新开课默认指向"，已存在的课程/报名持有
  自己的 `version_id` 快照。
* **容量一致性**：`courses` 行级锁串行化同一课程的席位决策；报名行再单独加行锁；
  取消/确认/清扫在同一加锁事务内完成"释放→统计→晋升"，配合
  `populate_existing()` 保证会话内为锁定后的最新状态。
* **清扫**：逐课程加锁、在一个事务内把所有超时席位置 `expired` 并按 FIFO 晋升，
  部分失败不影响其它课程；生产由后台任务周期执行，测试由维护接口驱动。
* **时间**：业务时间一律走 `app/clock.py`（单例行 `app_clock.offset_seconds`），
  生产偏移为 0 即真实时间。

## 6. 目录结构

```
app/
  main.py            FastAPI、异常映射、后台清扫任务
  config.py database.py clock.py errors.py models.py schemas.py deps.py seed.py
  services/          programs · enrollments · progress · certificates
  routers/           users · programs · courses · progress · admin
alembic/             0001_initial 初始迁移（含 app_clock 种子行）
tests/               7 个测试文件
Dockerfile docker-compose.yml entrypoint.sh requirements.txt
```
