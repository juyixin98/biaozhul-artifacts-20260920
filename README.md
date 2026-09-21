# CivicLedger

市政基金财务记账后端：分录过账、预算占用（encumbrance）、会计期间关闭。
技术栈：FastAPI + SQLAlchemy 2.0 + PostgreSQL，Docker Compose 一键启动。

**范围说明**：本系统刻意只做分录、预算占用和期间关闭。不包含薪资、不包含
整套采购系统，也不宣称满足任何会计法规认证。认证/合规相关内容（如
`X-User-Id` / `X-Role` 请求头的演示级鉴权）仅用于展示权限矩阵，不是生产级认证方案。

## 快速开始（Docker）

```bash
docker compose up -d --build
# API:      http://localhost:58001/docs   (Swagger UI)
# Postgres: localhost:55440 (user/password/db: civic/civic/civicledger)
```

启动时容器会自动执行 `alembic upgrade head`（迁移）和 `python -m scripts.seed`
（幂等样例数据：2 个基金、3 个部门、4 个科目、2026-09/10 两个开放期间、4 条预算行）。

> 端口 58001/55440 是为了避开本机常见端口占用，可在 `docker-compose.yml` 中修改。

## 本地开发与测试

```bash
pip install -r requirements.txt

# 测试（默认使用临时 SQLite 文件，全部自包含）
pytest

# 在真实 PostgreSQL 上跑（含并发预占、关账竞争测试）
docker compose up -d db
# 建一个干净的测试库后：
TEST_DATABASE_URL="postgresql+psycopg2://civic:civic@localhost:55440/civicledger_test" pytest

# 本地起服务
DATABASE_URL="postgresql+psycopg2://civic:civic@localhost:55440/civicledger" \
  sh -c 'alembic upgrade head && python -m scripts.seed && uvicorn app.main:app --reload'
```

## 角色与权限

通过请求头标识（演示级）：`X-User-Id: <名字>`，`X-Role: <角色>`。

| 角色 | 权限 |
|---|---|
| `accountant` | 过账/冲销分录、CSV 导入、创建/取消支出申请 |
| `finance_officer` | 以上全部，外加：建预算、批准申请、关闭/重开期间 |
| `auditor`（默认） | 只读：查询分录、预算、报表、导出、期间审计记录 |

## API 概览

| 方法与路径 | 说明 | 角色 |
|---|---|---|
| `POST /journals` | 过账分录（幂等键；重放返回 200 + 原结果，同键不同内容 409） | accountant+ |
| `GET /journals?year=&month=` / `GET /journals/{id}` | 查询分录及借贷明细 | 所有 |
| `POST /journals/{id}/reverse` | 在开放期间追加关联冲销凭证（借贷互换） | accountant+ |
| `POST /journals/import?year=&month=` | CSV 批量导入（text/plain 请求体） | accountant+ |
| `POST /budgets` / `GET /budgets?year=` | 预算行管理/查询 | officer / 所有 |
| `POST /expense-requests` | 创建支出申请 | accountant+ |
| `POST /expense-requests/{id}/approve` | 批准并预占预算 | finance_officer |
| `POST /expense-requests/{id}/cancel` | 取消并释放预占（仅一次） | accountant+ |
| `GET /expense-requests/{id}` | 申请详情（含入账凭证 `journal_entry_id`，可溯源） | 所有 |
| `POST /periods` / `GET /periods` | 期间管理/查询 | officer / 所有 |
| `POST /periods/{y}/{m}/close` | 关闭期间 | finance_officer |
| `POST /periods/{y}/{m}/reopen` | 重开期间（必须给 `reason`，留痕） | finance_officer |
| `GET /periods/{y}/{m}/audits` | 期间关闭/重开审计记录 | 所有 |
| `GET /reports/budget?year=` | 预算/预占/实际/可用额报表 | 所有 |
| `GET /reports/budget/export?year=` | 同上，CSV 导出 | 所有 |

## 核心设计

- **金额一律整数分**（`BIGINT`），单行/单笔上限 `999_999_999_999_999` 分。
  每条分录行借或贷恰好其一为正（数据库 CHECK 保证）。
- **借贷平衡与引用有效才可过账**：借方合计必须等于贷方合计且大于 0；
  基金/部门/科目编码必须存在且启用。
- **幂等过账**：`idempotency_key` 全库唯一。相同键 + 相同内容（规范化后
  SHA-256）重试返回原分录（HTTP 200）；同键不同内容返回 409。并发首发的
  唯一约束竞争通过 SAVEPOINT + 重读解决。
- **已过账分录不可变**：没有更新/删除端点（405）。纠错只能在开放期间
  追加冲销凭证（`reversal_of_id` 唯一，重复冲销 409）。
- **预算占用生命周期**：申请批准时 `encumbered += amount`；带
  `expense_request_id` 的分录过账时 `encumbered -= amount, actual += amount`
  （金额必须与申请一致）；取消时仅释放一次。所有变动都是带守卫条件的
  原子 `UPDATE ... WHERE`，并发批准不会超预算；`encumbered + actual <= amount`
  等 CHECK 约束是数据库级兜底，可用额不可能为负。
- **期间关闭与过账的明确顺序**：过账和关闭都先对期间行执行同一个带守卫的
  原子 `UPDATE`（`lock_version + 1`），行锁使二者串行化——要么过账先提交
  （关闭随后生效），要么关闭先提交（过账 409）。关闭后该期间拒绝一切写入。
  重开仅限财务负责人且必须填写原因，关闭/重开都写入 `period_audits` 留痕。
- **CSV 导入**：最多 2000 行数据行，按 `voucher_no` 归组，每组必须平衡；
  整个文件一个事务，任何一行错误整批回滚。重复导入同一文件是幂等的
  （键为 `csv:<期间>:<凭证号>`）。列：
  `voucher_no,fund_code,department_code,account_code,debit_cents,credit_cents,memo`。

## 项目结构

```
app/
  main.py            FastAPI 入口、异常映射
  config.py          DATABASE_URL 配置
  database.py        引擎/会话/Base
  models.py          基金/部门/科目/期间/分录/预算/申请/审计
  schemas.py         请求校验（含金额边界 MAX_CENTS）
  security.py        角色与权限依赖
  services/          journals.py（过账/冲销/CSV）、budgets.py（预占）、periods.py（关账）
  routers/           journals / budgets / periods / reports
alembic/             迁移（0001_initial 建全部表）
scripts/seed.py      幂等样例数据
tests/               36 个测试：借贷校验、批量回滚、重复冲销、并发预占、
                     关账竞争、金额边界、幂等、角色权限、报表与溯源
```

## 已知限制

- 演示级鉴权（请求头），生产环境需替换为真实认证与更细的授权。
- 单一货币、无多币种；无薪资与采购模块；未做会计法规合规认证。
- 报表为简单汇总，未提供多维分析。
