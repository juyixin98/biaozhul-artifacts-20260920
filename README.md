# Sentinel

本地员工活动异常检测服务：批量接收终端事件，按**事件发生时间**实时检测异常，
生成可分诊的警报。纯本地运行，除 PostgreSQL 外不依赖任何外部服务；
不包含桌面采集代理与前端。

技术栈：Python 3.12 · FastAPI · SQLAlchemy 2 · PostgreSQL 16 · Alembic · bcrypt · JWT

## 快速启动（Docker）

```bash
docker compose up --build
```

应用启动时自动执行 Alembic 迁移并写入演示数据（幂等）。服务地址：
http://localhost:8000 （交互式接口文档见 `/docs`）。

演示账号：

| 账号      | 密码        | 角色    | 数据范围                          |
|-----------|-------------|---------|-----------------------------------|
| `admin`   | `Admin123!`   | admin   | 全部警报                          |
| `manager` | `Manager123!` | manager | 仅直接下属（Alice/Bob/Carol）     |
| `analyst` | `Analyst123!` | analyst | 仅授权部门（Engineering）         |

## 本地开发

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg2://sentinel:sentinel@127.0.0.1:5432/sentinel_dev
alembic upgrade head          # 建表迁移
python -m scripts.seed_demo   # 演示数据（幂等）
uvicorn app.main:app --reload
```

运行测试（需要可连接的 PostgreSQL，测试库名由 `tests/conftest.py` 指定）：

```bash
pytest tests/ -q
```

## 接口说明

所有接口（除 `/health`、`/api/auth/login`）都需要 `Authorization: Bearer <token>`。

### 认证

- `POST /api/auth/login` — `{username, password}` → `{access_token}`。本地账号，bcrypt 校验。
- `GET /api/auth/me` — 当前用户信息。

### 事件摄取

- `POST /api/events/batch` — 请求体 `{"events": [...]}`，**每批 1–2000 条**。

事件字段：

| 字段          | 说明                                                                       |
|---------------|----------------------------------------------------------------------------|
| `device_id`   | 设备 ID（须已注册）                                                        |
| `event_id`    | 来源侧事件 ID；与 `device_id` 联合去重                                     |
| `event_type`  | `access` / `file_access` / `file_download` / `usb_connect`                 |
| `occurred_at` | 事件发生时间（**必须带时区偏移**），检测全部以该时间为准                    |
| `payload`     | 任意 JSON                                                                  |

语义：

- 以 `(device_id, event_id)` 去重；内容相同的重传记为 `duplicates`，幂等安全，
  并发重传也不会重复入库（唯一约束 + `ON CONFLICT DO NOTHING`）。
- 同 ID 但内容（类型/时间/载荷）不同 → 整批 `409` 并返回冲突项。
- 任意一项不合法（缺时区、未知设备、类型非法、超过 2000 条等）→ 整批 `422`，全部回滚。
- 响应：`{inserted, duplicates, alerts_created}`。

### 检测规则（按事件发生时间评估，与到达时间无关）

| 规则               | 触发条件                                                                 |
|--------------------|--------------------------------------------------------------------------|
| `off_hours_access` | `access` 事件发生在员工所属组织时区的 **[06:00, 22:00) 之外**；每人每天一条 |
| `download_burst`   | `file_download` 在窗口 **(t-10min, t]** 内超过 **50** 次（含 t，不含 10 分钟前边界） |
| `usb_first_use`    | 设备**首次** `usb_connect`（按事件时间最早者；每台设备只报一次）            |
| `baseline_anomaly` | 当日 `file_access` 次数相对此前 **14 个完整日**基线的 z-score > **3.0**     |

迟到/乱序事件会重新计算其影响的所有窗口（例如迟到的下载事件会重算之后
10 分钟内每个窗口的计数）；警报按 `(员工, 规则, 窗口)` 唯一，重复计算不会重复报警。

### 基线

- 基线取事件发生日（组织时区）之前 14 个完整日的 `file_access` 日计数。
- 历史不足 14 天 → 状态 `insufficient_history`；14 天方差为零 → 状态 `zero_variance`；
  两种状态都明确记录、不产生警报。
- 每次评估写入一行新的 `baseline_versions`（含均值/方差/z 值/状态），**版本递增、
  从不覆盖**；警报 `evidence` 中保存触发时的基线版本号与统计量。
- 重新计算不会改动既有警报（包括其分诊状态与证据），即不会覆盖调查记录。
- `GET /api/baselines?employee_id=` — 按警报同样的可见范围查询基线记录。

### 警报分诊

- `GET /api/alerts` — 列表，支持 `status` / `rule` / `employee_id` / `limit` / `offset` 过滤。
- `GET /api/alerts/{id}` — 详情（含证据）。
- `PATCH /api/alerts/{id}` — `{status, version}`，`status ∈ open|confirmed|false_positive|investigated`。
  携带当前版本号做乐观锁：版本不一致 → `409`，并发分诊只有一个赢家。

可见范围：`admin` 全部；`manager` 仅直接下属；`analyst` 仅授权部门
（`analyst_departments` 表）。越权访问返回 `403`。

## 项目结构

```
app/
  main.py            FastAPI 入口
  config.py          环境变量配置（DATABASE_URL / JWT_SECRET / ...）
  models.py          SQLAlchemy 模型（唯一约束即幂等与去重的基石）
  schemas.py         Pydantic 请求/响应模型（批次 ≤2000、时区校验）
  security.py        bcrypt 口令散列 + JWT
  deps.py            认证依赖与 RBAC 数据范围
  routers/           auth / events / alerts / baselines
  services/
    ingestion.py     批量摄取：去重、冲突检测、整批事务
    detection.py     四类规则 + 基线 z-score 评估
    triage.py        乐观锁状态更新
alembic/             迁移（0001 初始 schema）
scripts/seed_demo.py 演示数据
tests/               pytest 套件（真实 PostgreSQL 上运行）
```

## 测试覆盖

- `test_ingestion.py` — 幂等重传、同 ID 冲突 409 整批回滚、非法项整批回滚、
  2000 条上限、乱序摄取、5 线程并发重传不重复入库。
- `test_detection.py` — 组织时区内 06:00/22:00 边界、50/51 阈值、10 分钟窗口
  边界排除、迟到事件重算窗口且不重复报警、USB 首连（含乱序）。
- `test_baseline.py` — 历史不足 / 零方差显式状态、z>3 报警与证据、
  z=3 边界不报警、重算保留调查状态且基线版本递增。
- `test_alerts_rbac.py` — 未认证 401、manager/analyst/admin 数据范围、
  越权 403、版本冲突 409、并发分诊唯一赢家。
