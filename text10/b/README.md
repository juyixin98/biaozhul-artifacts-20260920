# CivicLedger

市政财务记账后端：分录过账、预算占用、期间关闭。技术栈 FastAPI + SQLAlchemy + PostgreSQL。

**范围说明**：只做核心记账流程（分录、预算占用、期间管理），不含薪资、不做整套采购系统，也不宣称满足任何会计法规认证。

## 快速启动

```bash
docker compose up --build
```

启动时会自动执行 Alembic 迁移并写入演示数据（基金/部门/科目/2026 年 12 个期间/示例预算）。
API 文档：<http://localhost:18110/docs>（PostgreSQL 映射到宿主机 56510 端口）

本地裸机运行：

```bash
pip install -r requirements.txt
export DATABASE_URL=postgresql+psycopg://civicledger:civicledger@localhost:56510/civicledger
alembic upgrade head
python -m scripts.seed
uvicorn app.main:app --reload
```

## 认证（演示用）

请求头 `Authorization: Bearer <token>`，令牌即角色：

| 令牌（默认值） | 角色 | 权限 |
|---|---|---|
| `dev-manager-token` | 财务负责人 | 全部写操作 + 重开期间 |
| `dev-accountant-token` | 会计 | 过账、导入、预算申请流转 |
| `dev-auditor-token` | 审计员 | 只读 |

## 核心规则

- **分录**：每张凭证含基金、部门、科目、期间和借贷明细，金额一律为整数分。仅允许借贷平衡且引用有效的凭证过账；已过账凭证不可编辑或删除，纠错只能在开放期间追加关联的冲销凭证（每张凭证最多被冲销一次）。
- **幂等过账**：`POST /journals` 必须携带 `Idempotency-Key`。相同键+相同内容返回原结果（`idempotent_replay: true`）；相同键+不同内容返回 409。
- **CSV 导入**：`POST /journals/import`，最多 2000 行，按 `voucher_no` 归组为凭证，任何一行错误整批回滚。样例见 `sample_data/journals.csv`。
- **预算**：按 年度+基金+部门+科目 管理。支出申请批准时预占（`encumbered`），入账时释放预占并形成实际支出（`actual`），取消时预占只释放一次。可用额 = 预算 − 预占 − 实际，不能为负；并发批准通过行锁串行化，不会超支。
- **期间**：过账与关账共用期间行锁，并发时按明确的串行顺序生效；关闭后该期间拒绝写入。仅财务负责人可重开，必须说明原因，操作写入审计日志。
- **查询**：`GET /reports/budget` 展示预算、预占、实际、剩余额；`GET /reports/budget/export` 导出 CSV；`GET /journals/{id}` 可追溯原始凭证；`GET /audit-log` 查看操作留痕。

## 测试

```bash
docker compose --profile test run --rm test
```

或本地（需可访问的 PostgreSQL，设置 `TEST_DATABASE_URL`）：

```bash
pip install -r requirements-test.txt
pytest -v
```

覆盖：借贷校验与金额边界、批量整批回滚、重复冲销、并发预占不超支、关账与过账竞争、幂等键语义、审计员只读。

## 目录结构

```
app/
  main.py            FastAPI 入口
  models.py          SQLAlchemy 模型
  security.py        Bearer 令牌与角色
  services/          posting / budget / periods / importer 业务核心
  routers/           journals / budgets / periods / reports
alembic/             迁移
scripts/seed.py      演示数据（幂等）
sample_data/         CSV 导入样例
tests/               pytest 测试
```
