# 流程编排引擎 Workflow Orchestration Engine

基于 **FastAPI + SQLAlchemy 2.0 (async) + PostgreSQL** 的轻量审批流引擎。
无消息队列、无外部服务依赖；超时升级由进程内轮询 worker 完成。

## 功能特性

- **JSON 流程定义**：`start` / `approval` / `condition` / `end` 四类节点。
  - 审批节点支持 **全签（all）** 与 **任签（any）**。
  - 发布前校验：缺失引用、不可达节点、环路、重复节点、条件表达式语法。
  - 条件为**受限表达式**（AST 白名单手工求值，永不 `eval`），禁止任意代码执行。
- **模板版本化、发布即不可变**：每次发布/回滚都追加新版本行，从不覆盖。
  新实例绑定指定版本（或最新版本）；修改/回滚只影响之后创建的实例，
  运行中的实例始终按其锁定版本执行。
- **幂等审批**：审批请求必须携带 `X-Request-Id` 与 `expected_version`。
  重复请求返回原结果；同 request id 用于不同实例/动作返回 `409`，且**不写入历史**。
- **会签竞争安全**：实例行 `SELECT … FOR UPDATE` + PostgreSQL 咨询锁，
  并发审批、撤回、超时升级只能产生一次有效状态转换；状态、待办、审计、幂等记录同事务提交。
- **拒绝规则明确**：任一指定审批人明确拒绝 → 流程立即终止为 `rejected`，
  其余待办关闭，`reject_reason` 可查询。
- **任签一人通过即推进**：其余待办自动关闭并写 `task_closed` 审计。
- **撤回**：提交人可在结束前撤回；撤回后待办关闭、拒绝后续操作。
- **权限**：只有节点待办上的指定审批人能操作；替换实例 ID 不能越权。
- **超时升级**：任务超时后关闭原待办、给升级人生成新待办（唯一决策人）；
  worker 失败自动重试，服务重启后继续处理未完成升级，且最多升级一次。
- **可观测**：返回当前位置（节点 + 待办）、完整历史、拒绝原因；OpenAPI 文档内置。

不在范围内：表单设计器、电子签名、多业务模块、消息队列/外部回调。

## 目录结构

```
app/
  main.py          FastAPI 应用与生命周期（启动 worker）
  config.py        环境变量配置
  db.py / models.py 引擎与 ORM
  validation.py    模板发布校验（引用/可达性/环路/表达式）
  expressions.py   受限条件表达式（AST 白名单）
  engine.py        全部状态转换（发布/发起/审批/撤回/升级）
  idempotency.py   request id 咨询锁与幂等记录
  worker.py        进程内超时升级轮询
  api/             HTTP 路由
alembic/           数据库迁移
demo/              演示流程 JSON + walkthrough 脚本
tests/             pytest 测试（43 个）
```

## 快速开始（Docker）

```bash
docker compose up --build
# API:  http://localhost:8000
# 文档: http://localhost:8000/docs
```

Compose 启动 Postgres 16、执行 Alembic 迁移、启动 API（worker 同进程）。

灌入演示模板：

```bash
curl -X POST http://localhost:8000/demo/setup
python demo/walkthrough.py --base-url http://localhost:8000
```

## 本地开发（不使用 Docker）

需要 PostgreSQL，并创建库（示例）：

```sql
CREATE USER workflow WITH PASSWORD 'workflow' CREATEDB;
CREATE DATABASE workflow OWNER workflow;
```

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export DATABASE_URL='postgresql+asyncpg://workflow:workflow@localhost:5432/workflow'
alembic upgrade head
uvicorn app.main:app --reload
```

跑测试（每个用例使用独立临时库并执行迁移）：

```bash
pytest -q
# 可用 TEST_DATABASE_URL 覆盖管理员连接串
```

## 流程定义格式

```json
{
  "nodes": [
    {"id": "start", "type": "start", "next_node": "manager"},

    {"id": "manager", "type": "approval", "name": "经理审批",
     "strategy": "any",
     "approvers": ["manager1", "manager2"],
     "next_node": "amount",
     "timeout_seconds": 30,
     "escalation_target": "boss"},

    {"id": "amount", "type": "condition",
     "branches": [
       {"expression": "amount >= 10000", "next_node": "countersign"},
       {"expression": "amount >= 1000",  "next_node": "finance"}
     ],
     "default": "end"},

    {"id": "countersign", "type": "approval",
     "strategy": "all", "approvers": ["f1", "f2"], "next_node": "end"},
    {"id": "finance", "type": "approval",
     "strategy": "any", "approvers": ["f1", "f2"], "next_node": "end"},
    {"id": "end", "type": "end"}
  ]
}
```

- `start` 必须且仅一个；至少一个 `end`；节点 id 唯一。
- 审批：`strategy` ∈ `all|any`，`approvers` 非空且不重复；
  `timeout_seconds` 与 `escalation_target`（升级人用户名，非节点）必须同时出现。
- 条件：从上到下短路匹配第一个为真的分支，都不匹配走 `default`；
  无 `default` 且无匹配会在运行时返回 `409 no_matching_branch`。

### 受限表达式语法

```
or_expr   := and_expr ("or" and_expr)*
and_expr  := not_expr ("and" not_expr)*
not_expr  := "not" not_expr | comparison
comparison:= operand (("==" | "!=" | "<" | "<=" | ">" | ">=") operand)*
operand   := 变量名 | 数字 | 字符串 | true | false | None | 正负号
```

允许 `+ - * / // %` 算术与括号布尔组合。**禁止**：函数调用、属性访问
（`__class__` 等）、下标、lambda、import、任何非白名单字面量。
缺失变量按类 SQL 的 NULL 处理（`== None` 为真，与数值比较为假）。

## 接口文档

所有变更接口可带 `X-Request-Id`；**审批接口强制要求**。操作用户用 `X-User-Id` 头。
错误体统一为 `{"code": "...", "message": "..."}`。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/templates/{code}/versions` | 发布新版本（校验失败 `400`） |
| GET  | `/templates/{code}/versions` | 列出全部版本 |
| GET  | `/templates/{code}/versions/{v}` | 取指定版本 |
| POST | `/templates/{code}/rollback/{v}` | 回滚＝追加一份旧版本副本 |
| POST | `/instances` | 发起实例（可指定 `template_version`） |
| GET  | `/instances/{id}` | 当前位置 + 待办 + 历史 + 拒绝原因 |
| POST | `/instances/{id}/decision` | 审批（见下） |
| POST | `/instances/{id}/withdraw` | 提交人撤回 |
| POST | `/demo/setup` | 灌入演示模板 |
| GET  | `/health` | 健康检查 |

### 发布模板

`POST /templates/expense/versions`
```json
{"name": "报销", "definition": { "nodes": [ ... ] }}
```
→ `201` 返回含自增 `version` 的不可变版本。

### 发起实例

`POST /instances`（头 `X-User-Id: alice`）
```json
{
  "template_code": "expense",
  "template_version": 3,
  "business_key": "EXP-20260920-001",
  "variables": {"amount": 50000}
}
```
省略 `template_version` 则绑定最新版本。`(template_code, business_key)` 唯一。
响应包含 `template_version`（后续审批的 `expected_version`）、`current_node_id`、
`pending_tasks`（含 `assignee`）和 `history`。

### 审批 / 拒绝

`POST /instances/{id}/decision`
头：`X-User-Id: manager1`、`X-Request-Id: <客户端生成的唯一值>`
```json
{"action": "approve", "expected_version": 3, "comment": "同意"}
{"action": "reject",  "expected_version": 3, "comment": "预算不足"}
```

行为：

| 场景 | 结果 |
|---|---|
| request id 缺失 | `400 missing_request_id` |
| `expected_version` 与实例版本不符 | `409 version_conflict`，不写历史 |
| 非该节点审批人 / 替换实例 ID 越权 | `403 forbidden` |
| 重复 request id | 返回首次结果（200，同一响应体） |
| 同 request id 换动作/实例 | `409 request_id_conflict`，不写历史 |
| 任签：一人通过 | 流程推进，其余待办 `closed` |
| 全签：全部通过 | 流程才推进 |
| 全签/任签：一人明确拒绝 | 立即 `rejected`，其余待办关闭 |
| 实例已结束后再操作 | `409 instance_not_running` |
| 自己的待办已处理后重复提交 | `409 task_already_decided` |

### 撤回

`POST /instances/{id}/withdraw`（头为提交人本人 `X-User-Id`）
```json
{"comment": "信息填错了"}
```
仅提交人、且实例仍在运行时可撤回；待办全部关闭，状态变为 `withdrawn`。

### 查询实例

`GET /instances/{id}` 返回：

- `status`：`running | approved | rejected | withdrawn`
- `current_node_id`：当前节点（结束时为对应 end 节点）
- `pending_tasks`：当前节点仍待处理的任务（id、assignee、due_at、是否已升级）
- `reject_reason`：被拒绝时的原因
- `history`：`started / enter_node / approved / rejected / task_closed /
  escalated / withdrawn` 等审计事件（含 actor、节点、详情、时间）

## 并发与一致性设计

- 审批/撤回/升级先对实例行 `SELECT … FOR UPDATE`，同实例的变更完全串行化；
  request id 用 `pg_advisory_xact_lock` 串行化，幂等记录与业务变更同事务提交。
- 版本号分配使用按 template_code 的咨询锁（首版本并发发布也安全）。
- 超时升级在锁内复查任务仍 pending、实例仍 running、节点未前移且未升级过，
  因此任务失败重试、多 worker、重启都不会重复升级。
