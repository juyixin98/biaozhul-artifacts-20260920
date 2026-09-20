# 流程编排引擎（Workflow Orchestration Engine）

基于 **FastAPI + SQLAlchemy 2.0 + PostgreSQL** 的审批流程引擎，不依赖消息队列或任何外部服务。
支持 JSON 流程定义（开始 / 审批 / 条件分支 / 结束）、会签与任签、模板版本不可变、
幂等审批、提交人撤回、超时升级与重启恢复。

## 1. 快速开始

### Docker（推荐）

```bash
docker compose up --build
# API:    http://localhost:18088  (Swagger: /docs)
# 首次启动自动执行 Alembic 迁移并播种演示模板 expense
```

> `docker-compose.yml` 默认把 API 映射到主机 **18088** 端口（如冲突可自行修改）；
> PostgreSQL 不对主机暴露，仅供 compose 网络内的 API 访问。

### 本地运行

```bash
pip install -r requirements.txt

# 准备数据库
createdb wfdb
export WF_DATABASE_URL="postgresql+psycopg2://wfuser:wfpass@127.0.0.1:5432/wfdb"

alembic upgrade head                # 建表
python -c "from app.db import SessionLocal; from app.seed import seed_demo; \
db=SessionLocal(); seed_demo(db); db.close()"   # 可选：演示模板

uvicorn app.main:app --reload
```

## 2. 演示流程

`POST /demo/seed` 幂等创建已发布的 `expense` 模板（v1）：

```
提交报销(start)
  → 经理会签(approval, mode=all, manager1+manager2)
  → 金额条件(condition)
       amount >= 10000 → 总监任签(approval, mode=any, director1|director2,
                               timeout 60s 未处理则升级给 cfo)
                         → 通过(end:approved)
       默认            → 预算条件(condition)
                          budget_status == 'frozen' → 预算驳回(end:rejected)
                          默认                      → 通过(end:approved)
```

任何审批人**明确拒绝**会立即结束流程（`rejected`），记录拒绝原因并关闭其余待办。

## 3. JSON 流程定义

```json
{
  "nodes": [
    {"id": "s", "type": "start", "name": "开始"},
    {"id": "ap", "type": "approval", "name": "经理审批",
     "mode": "all", "assignees": ["manager1", "manager2"],
     "timeout_seconds": 60, "escalate_to": ["director"]},
    {"id": "cond", "type": "condition", "name": "金额判断"},
    {"id": "ok", "type": "end", "name": "通过", "terminal": "approved"},
    {"id": "no", "type": "end", "name": "驳回", "terminal": "rejected"}
  ],
  "edges": [
    {"source": "s",    "target": "ap"},
    {"source": "ap",   "target": "cond"},
    {"source": "cond", "target": "ap",   "expression": "amount >= 10000"},
    {"source": "cond", "target": "ok"}
  ]
}
```

节点类型：

| type | 字段 | 出边规则 |
|---|---|---|
| `start` | — | 恰好 1 条 |
| `approval` | `mode`：`all`(全签)/`any`(任签)；`assignees`；可选 `timeout_seconds`+`escalate_to`（必须同时出现） | 恰好 1 条 |
| `condition` | — | ≥2 条，条件边带 `expression`，**恰好 1 条无表达式的默认边** |
| `end` | `terminal`：`approved`/`rejected` | 无出边 |

### 发布前校验（拒绝即返回 400，错误码在 `error.details[]`）

- 节点 id 重复、边引用不存在的节点、重复边
- 开始节点不是恰好 1 个、缺少结束节点、审批节点缺少 mode/assignees
- **不可达节点 / 孤儿节点 / 死路**、**有向环**（含自环）
- 条件节点缺少默认边、表达式重复
- 条件表达式在发布时做**编译期校验**（只解析不执行）

### 受限表达式（绝不执行任意代码）

手写词法器 + 递归下降解析器实现，不使用 `eval/exec`：

- 运算：`+ - * / %`、比较 `== != > >= < <=`、逻辑 `and or not`、成员 `x in [...]`
- 字面量：数字、单/双引号字符串、`true false null`、列表
- 变量：实例 payload 的**点路径读取**，如 `form.region`；缺失即 `null`
- 白名单纯函数：`len`、`lower`、`upper`
- 禁止：函数调用白名单外的函数（`eval/open/__import__…` 发布即拒）、
  dunder 名称（`__class__/__globals__`）、属性方法调用、赋值、lambda、三元表达式

## 4. 版本不可变与隔离

- 版本创建时为 `draft`，发布（publish）后**定义与状态永不修改**。
- `templates.current_version` 只是**指针**：新发布会前移；回滚 `POST /templates/{key}/rollback/{v}`
  把指针指回某个**已发布**旧版本，不删除/不改写任何版本。
- 启动实例时绑定版本（显式 `version` 或当前发布版），并**冻结定义快照**。
  之后发布新版本或回滚，只影响**之后创建**的实例；运行中的实例永远按原版本执行。
- 审批携带 `expected_version`，与实例绑定版本不一致返回 `409 version_conflict`，不写历史。

## 5. 审批、幂等与状态机

`POST /decisions`

```json
{
  "request_id": "客户端生成的唯一ID",
  "instance_id": "uuid",
  "expected_version": 1,
  "actor": "manager1",
  "decision": "approve | reject",
  "comment": "可选"
}
```

- **幂等**：`request_id` 全局唯一。重复请求（全部参数一致）返回首次的原结果，
  响应中 `"replayed": true`，**历史只写一次**。
- **冲突不写入**：同一 `request_id` 但实例/版本/人/决定/备注不一致 → `409 idempotency_conflict`，不落任何记录。
- **全签(all)**：每个审批人都通过才推进；尚有未决待办时返回 `waiting_for_others`。
- **任签(any)**：任一人通过即推进，其余待办自动置为 `closed`（历史含 `tasks_closed`）。
- **明确拒绝(reject)**：流程立即 `rejected`，关闭同代其他待办，
  `reject_reason` 形如 `manager2 明确拒绝: 预算不足`。
- **权限**：只有当前节点 `assignees` 中的人能操作（越权 → `403 not_assignee`）；
  待办已处理后再操作 → `409 task_already_handled`。更换 instance_id 不能越权。
- 撤回 `POST /withdrawals`：仅提交人本人、流程结束前可撤回；同样以 `request_id` 幂等。

### 并发与事务

- 所有状态迁移先对实例行 `SELECT … FOR UPDATE`，并对当期待办加行锁，
  并发审批/撤回/超时升级因此**串行化，最多一次有效状态转换**。
- 状态、待办、审计历史、升级调度记录在**同一事务**提交。
- 待办表有部分唯一索引：同一节点激活代次中，同一审批人最多一条 `pending`。

## 6. 超时升级（无消息队列）

- 每个待触发升级的节点激活时写一行 `scheduled_escalations`（`due_at`、`generation`、`done`）。
- 进程内守护线程每 `WF_SCHEDULER_INTERVAL_SECONDS`（默认 1s）用
  `FOR UPDATE SKIP LOCKED` 抢占到期任务，每个任务独立事务处理。
- 触发时：旧代待办置 `escalated` → 用 `escalate_to` 名单开启新一代待办（generation+1），
  审计 `tasks_escalated`。升级在节点维度**只发生一次**。
- 任务处理失败只回滚该任务（仍为 `done=false`），下一轮自动**重试**；
  服务重启后扫描线程直接从该表继续，未完成升级不丢失。

## 7. API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/demo/seed` | 幂等播种演示模板 |
| POST | `/templates` | 创建模板（含 v1 draft，创建即校验） |
| GET | `/templates` / `/templates/{key}` | 列表 / 详情（含版本） |
| POST | `/templates/{key}/versions` | 新建 draft 版本（发布前可反复重建） |
| GET | `/templates/{key}/versions/{v}` | 查看某版本定义 |
| POST | `/templates/{key}/versions/{v}/publish` | 发布（不可变） |
| POST | `/templates/{key}/rollback/{v}` | 回滚当前指针到已发布版本 |
| POST | `/instances` | 启动实例（绑定版本+冻结快照） |
| GET | `/instances/{id}` | 当前位置、状态、打开的待办、拒绝原因 |
| GET | `/instances/{id}/history` | 完整审计历史（按序号） |
| POST | `/decisions` | 审批（通过/拒绝，幂等） |
| POST | `/withdrawals` | 提交人撤回（幂等） |

交互式文档：`/docs`（Swagger）、`/redoc`。错误响应统一为：

```json
{"error": {"code": "not_assignee", "message": "...", "details": null}}
```

## 8. 测试

```bash
export WF_TEST_DATABASE_URL="postgresql+psycopg2://wfuser:wfpass@127.0.0.1:5432/wfdb"
pytest
```

覆盖：

- `test_template_validation` — 缺失引用、不可达、环路、条件默认边、危险表达式、不可变性
- `test_version_isolation` — 版本绑定、发布后旧实例不变、回滚只影响新实例、版本冲突不写历史
- `test_counter_sign` — 全签等待/齐签推进、任一拒绝终结、任签关闭其余待办、越权 403、并发竞争
- `test_idempotency` — 重复返回原结果、参数冲突 409 不写历史、撤回幂等、完成后不可撤回
- `test_timeout_recovery` — 升级换人、一次性、审批后任务失效、重启续跑、并发只转换一次、失败重试
- `test_conditions` / `test_demo` — 条件分支、表达式安全、端到端冒烟

## 9. 目录结构

```
app/
  main.py          FastAPI、异常处理、生命周期(调度器)
  config.py db.py  配置 / Engine / Session
  models.py        7 张表（模板、版本、实例、待办、幂等请求、历史、升级队列）
  schemas.py       Pydantic 定义与 API 模型
  expression.py    受限表达式词法/语法/求值器
  validator.py     发布前结构校验
  catalog.py       模板版本/发布/回滚
  engine.py        运行时：遍历、会签/任签、幂等、撤回、升级
  scheduler.py     后台升级线程（抢占/重试/重启恢复）
  serializers.py seed.py
migrations/        Alembic 迁移（0001_initial，支持 up/down）
tests/             pytest 套件
scripts/           Docker entrypoint（等待 DB→迁移→播种→启动）
```

## 10. 明确不做

表单设计器、电子签名、多业务模块/多租户、消息队列与外部工作服务——均不在范围内。
