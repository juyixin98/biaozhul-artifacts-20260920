# CivicLedger

面向市政基金的财务记账后端：会计分录、预算占用（encumbrance）与会计期间关闭。
技术栈：FastAPI + SQLAlchemy 2 + PostgreSQL，Docker Compose 一键启动。

> 范围说明：本系统只做分录、预算占用和期间管理，不包含薪资、完整采购流程，
> 也未通过也不宣称满足任何会计法规认证（如 GASB/FASB 合规认证）。

## 快速开始

```bash
docker compose up -d            # 启动 Postgres + API (http://localhost:8000)
docker compose exec app python -m samples.seed   # 可选：写入样例基础数据
docker compose run --rm test    # 运行测试（独立测试库，含并发用例）
```

API 文档：http://localhost:8000/docs

本地开发（需本机 PostgreSQL）：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://civic:civic@localhost:5432/civicledger
alembic upgrade head
uvicorn app.main:app --reload
TEST_DATABASE_URL=postgresql+psycopg://civic:civic@localhost:5432/civicledger_test pytest
```

## 角色（请求头）

| 请求头 | 说明 |
|---|---|
| `X-User-Name` | 用户名，写入 created_by / 审计日志 |
| `X-User-Role` | `clerk` / `approver` / `finance_manager` / `auditor`（缺省 auditor） |

- **clerk**：过账分录、CSV 导入、冲销
- **approver**：批准 / 取消支出申请（预占）
- **finance_manager**：以上全部 + 预算与期间管理、关账、**重开期间**
- **auditor**：只读（所有 GET、报表与导出）

## 核心规则

### 分录
- 金额一律为**整数分**（`debit_cents` / `credit_cents`，BIGINT，上限 9,999,999,999,999）。
- 每行必须且只能有一方为正；整笔借贷合计必须相等且大于 0；基金/部门/科目/期间必须存在。
- 过账即落库，**已过账分录不可编辑或删除**（API 不提供 PUT/DELETE）。
- **幂等键**：`idempotency_key` 唯一。相同内容重试返回原分录（HTTP 200）；
  同键不同内容返回 409 `idempotency-key-conflict`。唯一约束兜底并发竞态。
- **冲销**：`POST /journals/{id}/reverse` 在**开放期间**追加一张关联
  （`reversal_of_id`）的镜像分录；每笔只能被冲销一次（唯一约束 + 409）。

### CSV 批量导入
`POST /journals/import?year=2026&period=3`，请求头 `X-Idempotency-Key`，
multipart 上传 CSV，列：`voucher_no,fund_code,department_code,account_code,debit,credit`
（金额为元、最多两位小数）。按 `voucher_no` 归组为分录；**最多 2000 行**；
任何一行错误（引用无效、借贷不平、金额非法）→ 整批 422 回滚，一条不写。
幂等键为 `<X-Idempotency-Key>:<voucher_no>`，整批重试安全。

### 预算与预占
- 预算维度：年度 + 基金 + 部门 + 科目（唯一）。可用额 = 预算 − 预占 − 实际。
- 批准支出申请（`POST /encumbrances`）时预占：对预算行 `SELECT ... FOR UPDATE`，
  并发批准串行化，可用额不会变负（服务层校验 + CHECK 约束双保险）。
- 过账分录带 `encumbrance_id` 时核销：要求该预算维度上的费用借方合计等于预占额，
  释放预占并形成实际支出（可用额不变）。每个预占只能核销一次。
- 取消预占（`POST /encumbrances/{id}/cancel`）只释放一次，重复取消返回 409。
- 无预算维度的费用行不受控制；有预算的费用行（含冲销的负数）实时增减实际支出。

### 期间关闭
- 过账与关账都对期间行加 `FOR UPDATE` 锁：并发时按锁获取顺序**明确生效**——
  要么先过账后关账，要么关账后过账被拒（409 `period-closed`），无中间态。
- 只有 `finance_manager` 能关账/重开；重开必须填写原因（≥3 字符），
  关闭与重开都写入 `period_audit_logs`（`GET /periods/{id}/audits` 可查）。

### 查询与导出
- `GET /reports/budget?year=...`：预算、预占、实际、可用额。
- `GET /reports/budget/export?year=...`：同数据 CSV 导出。
- `GET /budgets/{id}/activity`：预算背后的预占单与分录行，可追溯到原始凭证
  （分录 id、幂等键、来源、冲销链）。

## 测试

覆盖：借贷平衡校验、无效引用、金额边界（0/负数/上限）、幂等重放与冲突、
CSV 整批回滚与 2000 行上限、重复冲销、取消只释放一次、并发预占不超预算、
关账/过账竞态、关闭期间禁写、重开权限与留痕、审计员只读。

```bash
docker compose run --rm test
```

## 目录结构

```
app/
  models.py            SQLAlchemy 模型（含 CHECK 约束）
  schemas.py           Pydantic 请求/响应（金额边界校验）
  security.py          基于请求头的角色控制
  services/            领域逻辑：journals / budgets / periods（锁与事务边界）
  routers/             HTTP 层：journals / budgets / periods / reports
alembic/               迁移（0001_initial）
samples/               种子脚本与样例 CSV
scripts/ensure_test_db.py  测试库创建（compose test 用）
tests/                 pytest（含线程并发用例，需 PostgreSQL）
```
