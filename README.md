# CivicLedger

面向小型公共机构的**基金会计记账后端**，聚焦三件事：

1. **分录（Journal Entries）**：基金 / 部门 / 科目 / 期间 + 借贷明细，金额一律使用**整数分**；
2. **预算占用（Budget Pre-occupancy）**：批准支出申请时预占额度，入账时释放预占并形成实际支出；
3. **期间关闭（Period Close）**：关账后禁止写入，仅财务负责人可凭原因重开并留痕。

> 技术栈：FastAPI + SQLAlchemy 2 + PostgreSQL 16 + Alembic。提供 Docker 启动方式。
>
> **范围声明**：本项目不包含薪资核算，也不是完整采购系统；**不宣称满足任何会计法规、审计准则或合规认证要求**。

## 快速开始（Docker）

```bash
docker compose up --build
```

启动时容器会自动等待数据库、执行 `alembic upgrade head` 并（`RUN_SEED=1` 时）载入样例数据。
API 位于 <http://localhost:8000>，交互式文档在 <http://localhost:8000/docs>。

数据库容器默认**不发布主机端口**（仅 compose 网络内可达）。API 主机端口可用
`CIVICLEDGER_HOST_PORT` 覆盖（默认 8000）：

```bash
CIVICLEDGER_HOST_PORT=58000 docker compose up --build
```

## 角色与认证

所有接口（除 `/health`）使用 `X-API-Key` 请求头。开发环境内置三个角色：

| 角色          | 开发 Key             | 权限                                     |
| --------------- | ---------------------- | ------------------------------------------ |
| `lead` 财务负责人 | `dev-key-lead`         | 主数据、预算、批准/取消申请、关账/重开、过账 |
| `accountant` 会计 | `dev-key-accountant`   | 过账、冲销、取消申请、读取                 |
| `auditor` 审计员  | `dev-key-auditor`      | **只读**                                   |

生产环境通过环境变量覆盖：

```bash
CIVICLEDGER_API_KEYS="lead:<random>,accountant:<random>,auditor:<random>"
```

## 本地开发（无 Docker API）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 任意可达的 PostgreSQL 16
export CIVICLEDGER_DATABASE_URL="postgresql+psycopg://USER:PASS@localhost:5432/civicledger"
alembic upgrade head
python -m app.seed            # 可选样例数据
uvicorn app.main:app --reload
```

## 核心规则

### 分录与校验

- 每条分录至少两行；每行**恰好**在借方或贷方有一侧为正（另一侧为 0）。
- 整笔分录借方合计必须等于贷方合计，且合计 > 0；单行/合计上限
  `10_000_000_000_00` 分（防止 BIGINT 溢出，见 `app/config.py`）。
- 引用的基金、部门、科目、期间必须存在，且 `entry_date` 必须落在期间日期范围内。
- 分录一经过账**不可编辑、不可删除**（服务不提供修改/删除接口）。
- 纠错方式：在**开放期间**追加一张关联冲销凭证（`POST /entries/reversal`，必须填写原因），
  系统按行镜像借贷方向，原始凭证与冲销凭证互相可追溯。一张凭证**只能被冲销一次**，
  冲销凭证本身不能再被冲销。
- CSV 批量上传（`POST /entries/csv`）：
  - 数据行上限 **2000**，按 `voucher_no` 归组；
  - 表头必须为：`voucher_no,entry_date,period_code,description,line_no,fund_code,department_code,account_code,debit_cents,credit_cents,reservation_request_no,line_description`；
  - **任何一行错误，整批回滚**，错误以行号 + 凭证号返回。

### 幂等键

`POST /entries`、`POST /entries/reversal`、`POST /entries/csv`、
`POST /reservations`、`POST /reservations/{no}/cancel` 均支持 `Idempotency-Key` 请求头：

- 同键 + 同请求体（规范化 JSON 哈希）→ 返回**首次的原始结果**，不重复入账；
- 同键 + 不同请求体 → **409 idempotency_conflict**。

### 预算与预占

- 预算按**年度 + 基金 + 部门 + 费用科目（account_class=5）**唯一管理（`PUT /budgets`）。
- 批准支出申请（`POST /reservations`，仅 lead）→ 预算行 `SELECT … FOR UPDATE`
  加锁，校验 `预算 ≥ 预占 + 实际 + 本次申请`，通过后 `reserved += 金额`。
  并发批准在同一预算行上串行，**不可能超预算**。
- 过账时，费用科目借方行可引用一笔申请：校验金额与范围完全一致，
  入账后 `reserved -= 金额`、`actual += 金额`，申请状态变为 `consumed`。
- 取消申请（`POST /reservations/{no}/cancel`）只在申请仍为 `approved` 时释放一次；
  对已消费/已取消/已冲销的申请返回 409。
- 冲销已消费的申请：费用以贷方冲回、`actual -= 金额`、`reserved += 金额`，
  申请状态变为 `reversed`，并关联到冲销凭证。
- 任何计数器变化都保证 `reserved ≥ 0`、`actual ≥ 0`、
  `reserved + actual ≤ amount`（**可用额不为负**）。

### 期间关闭

- 过账与关账都以 `SELECT … FOR UPDATE` 锁定同一期间行，加锁顺序全局固定
  （期间 → 预占 → 预算），因此两者竞争时结果**严格有序**：
  先拿到锁的过账会在关账标志置位前提交；关账先拿到锁则过账被拒（409）。
- 关闭后该期间禁止任何写入（包括冲销）。
- 仅 `lead` 可重开，**必须提供原因**；关闭与重开都写入 `period_events` 留痕，
  可通过 `GET /periods/{code}/events` 审计。

### 查询、导出与追溯

- `GET /reports/budget-usage`（支持 `.csv`）展示每个预算范围的预算额、预占额、
  实际额、剩余额。
- `GET /reports/budget-trace?year=&fund_code=&department_code=&account_code=`
  返回该预算范围内每一条已过账分录行（含凭证号、是否冲销、冲销对象、关联申请号）
  及全部申请记录，数字可逐笔追溯到原始凭证。
- 审计员对以上接口及分录/预算/期间列表均有读取权限。

## API 概览

见 <http://localhost:8000/docs>（OpenAPI 自动生成）。主要端点：

| 方法   | 路径                                        | 角色                 |
| ------ | ------------------------------------------- | -------------------- |
| PUT    | `/funds/{code}` `/departments/{code}` `/accounts/{code}` | lead      |
| POST   | `/periods`                                  | lead                 |
| POST   | `/periods/{code}/close` `/reopen`           | lead                 |
| GET    | `/periods` `/periods/{code}/events`         | 全部（只读）         |
| PUT    | `/budgets`                                  | lead                 |
| POST   | `/reservations`                             | lead                 |
| POST   | `/reservations/{request_no}/cancel`         | lead / accountant    |
| POST   | `/entries`                                  | lead / accountant    |
| POST   | `/entries/reversal`                         | lead / accountant    |
| POST   | `/entries/csv`                              | lead / accountant    |
| GET    | `/entries`                                  | 全部（只读）         |
| GET    | `/reports/budget-usage[.csv]`               | 全部（只读）         |
| GET    | `/reports/budget-trace`                     | 全部（只读）         |

请求/响应字段见 `app/schemas.py`。

## 数据库迁移

```bash
alembic upgrade head      # 应用
alembic downgrade base    # 回滚
```

初始迁移 `0001_initial` 与 SQLAlchemy 模型（`app/models.py`）一致。

## 测试

需要一个可达的 PostgreSQL（测试会自动创建测试库）：

```bash
export CIVICLEDGER_TEST_DATABASE_URL="postgresql+psycopg://civicledger:civicledger@localhost:5432/civicledger_test"
export CIVICLEDGER_ADMIN_DATABASE_URL="postgresql+psycopg://civicledger:civicledger@localhost:5432/postgres"
pytest
```

36 个测试覆盖：

- 借贷平衡、单边行校验、金额上限边界；
- CSV 整批回滚、2000 行上限、错误编码；
- 幂等键重放与同键异内容冲突；
- 重复冲销拒绝、冲销恢复预占；
- 并发批准不超预算、取消与入账竞争；
- 关账/过账竞争的确定性顺序、关账后禁写、重开留痕；
- 审计员只读、预算追溯与 CSV 导出。

## 目录结构

```
app/
  main.py            # FastAPI 应用与错误处理
  config.py          # API Key、金额上限、CSV 行数上限
  database.py        # 引擎/Session
  models.py          # SQLAlchemy 模型
  schemas.py         # Pydantic 入参/出参
  errors.py          # 领域错误（422/409/404/403/401）
  security.py        # X-API-Key 与角色
  seed.py            # 幂等样例数据
  routers/           # HTTP 路由
  services/
    __init__.py      # 幂等键执行器
    entries.py       # 过账 / 冲销 / 预算联动（行锁）
    budgets.py       # 预算 upsert / 申请批准 / 取消
    periods.py       # 关账 / 重开 / 事件
    csv_upload.py    # CSV 解析与整批过账
alembic/             # 迁移
samples/entries.csv  # 批量上传样例
tests/               # pytest
Dockerfile docker-compose.yml docker-entrypoint.sh
```

## 设计取舍与非目标

- 金额使用整数分，避免浮点误差；不处理多币种、汇率。
- 预算只覆盖**费用科目的借方发生额**；负债/资产类分录不消耗预算。
- 认证使用静态 API Key（足够区分三个内部角色），不含用户管理、SSO。
- 不生成法定财务报表、不做税务处理、不做薪资/采购全流程，亦不声明任何法规符合性。
