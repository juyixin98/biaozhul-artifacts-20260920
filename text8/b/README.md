# 流程编排引擎（FastAPI + SQLAlchemy + PostgreSQL）

用 JSON 定义 **开始 / 审批（全签、任签）/ 条件分支 / 结束** 节点的轻量审批流引擎。
不依赖消息队列或外部调度服务，超时升级由内置后台线程 + 数据库行锁完成。

- 发布前校验：缺失引用、不可达节点、环路、非法/危险表达式一律拒绝发布
- 模板版本发布后**不可变**；实例绑定具体版本，修改/回滚只影响之后发起的实例
- 审批携带 `request_id`（幂等）与 `expected_version`（乐观版本）；冲突不写历史
- 全签全部通过才推进；任签一人通过即推进并关闭其余待办；明确拒绝按 `on_reject` 处理
- 并发审批 / 撤回 / 超时升级通过 `SELECT … FOR UPDATE [SKIP LOCKED]` 保证**只产生一次**
  有效状态转换，状态、待办、审计同事务提交
- 提交人可在结束前撤回；只有待办的指定审批人能操作，无法用替换实例 ID 越权
- 超时支持升级（仅一次）、自动通过、自动拒绝；任务失败可重试，重启后继续处理
- 受限表达式：AST 白名单递归求值，**不调用 eval**，禁止函数调用/属性/下标等

## 快速开始（Docker）

```bash
docker compose up --build
# 服务启动时自动：等待 DB -> alembic 迁移 -> 种子演示模板 -> 启动 API + 超时扫描线程
```

- API 文档（Swagger）：http://localhost:8000/docs
- ReDoc：http://localhost:8000/redoc
- 健康检查：`GET /health`
- 演示模板会自动种入（`SEED_DEMO_ON_START=true`），也可手动 `POST /demo/seed`

## 本地开发（不用 Docker）

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# 准备数据库
createdb workflow                       # 或使用 docker compose up db
export DATABASE_URL='postgresql+psycopg2://workflow:workflow@localhost:5432/workflow'

alembic upgrade head                    # 建表
ENABLE_SWEEPER=true SEED_DEMO_ON_START=true \
  uvicorn workflow_engine.main:app --reload

# 测试（需要 PostgreSQL，连接串由 DATABASE_URL 指定）
pytest -q
```

## 模板定义示例

见 [`demo/expense_demo.json`](demo/expense_demo.json)：

```json
{
  "key": "expense",
  "name": "报销审批流程",
  "nodes": [
    {"id": "start", "type": "start", "next": "manager"},
    {
      "id": "manager", "type": "approval", "name": "主管会签",
      "mode": "all", "approvers": ["alice", "bob"],
      "on_reject": "rejected_end",
      "timeout_seconds": 3600,
      "timeout_action": {"type": "escalate", "to": ["carol"]},
      "next": "amount_check"
    },
    {
      "id": "amount_check", "type": "condition",
      "branches": [{"when": "amount > 10000", "next": "cfo"}],
      "default": "approved_end"
    },
    {"id": "cfo", "type": "approval", "name": "CFO 任签", "mode": "any",
     "approvers": ["cfo1", "cfo2"], "on_reject": "rejected_end", "next": "approved_end"},
    {"id": "approved_end", "type": "end", "outcome": "approved"},
    {"id": "rejected_end", "type": "end", "outcome": "rejected"}
  ]
}
```

节点字段：

| 节点 | 必填 | 说明 |
| --- | --- | --- |
| `start` | `next` | 有且仅有一个 |
| `approval` | `name, mode(all/any), approvers, next` | `on_reject` 默认终止为 rejected；`timeout_seconds` + `timeout_action` 成对出现；升级目标不得与原审批人重叠 |
| `condition` | `branches[].when/next` | 必须有 `default` 或恒真分支（`true`），避免悬空；`when` 为受限表达式 |
| `end` | `outcome=approved/rejected` | 不允许出边 |

受限表达式允许：字面量、上下文变量、`+ - * / // % **`（指数受限）、
比较（`== != < <= > >= in not in is is not`）、`and/or/not`、三元、`list/tuple`。
禁止：函数调用、属性访问、下标、lambda、推导式、f-string、海象运算符等。
未知变量在**运行期求值时**报错（发布期只做语法白名单检查）。

## 主要流程（cURL）

```bash
# 1. 发布模板
curl -s localhost:8000/templates -H 'Content-Type: application/json' \
  -d '{"definition": '"$(cat demo/expense_demo.json)"', "publish": true}'

# 2. 发起实例（X-User 即提交人；可指定 "version": 1，否则取当前版本）
curl -s localhost:8000/instances -H 'X-User: tom' -H 'Content-Type: application/json' \
  -d '{"template_key":"expense","title":"出差报销","context":{"amount":300}}'

# 3. 审批（X-User 是审批人；node_id 或 ?task_id= 指定待办）
curl -s -XPOST 'localhost:8000/instances/1/decide?node_id=manager' \
  -H 'X-User: alice' -H 'Content-Type: application/json' \
  -d '{"request_id":"req-001","expected_version":1,"action":"approve"}'

# 4. 查询当前位置 / 待办 / 历史 / 拒绝原因
curl -s localhost:8000/instances/1

# 5. 提交人撤回
curl -s -XPOST localhost:8000/instances/1/withdraw \
  -H 'X-User: tom' -H 'Content-Type: application/json' \
  -d '{"request_id":"wd-001","expected_version":1}'
```

接口完整说明见 [`docs/api.md`](docs/api.md)，行为规格见 [`docs/design.md`](docs/design.md)。

## 版本与回滚

- `POST /templates`（`publish:true` 直接发布 v1）；`POST /templates/{key}/versions` 发新版本；
  草稿用 `publish:false` 创建，再 `POST /templates/{key}/versions/{v}/publish`。
- `POST /templates/{key}/rollback/{v}` 只把"当前版本指针"指回任一已发布版本，
  **不改写任何定义**。运行中的实例绑定的是 `version_id`，指针变化与它无关。

## 幂等与冲突语义

- 每个审批/撤回请求必须带 `request_id`；重复请求返回**首次成功响应**，
  响应里 `"idempotent_replay": true`。
- 业务冲突（409：版本不符、无权限、流程已结束等）与台账、状态、审计**同事务回滚**，
  不写历史、不占用 `request_id`，修正后可以复用同一 ID 重试。

## 超时升级

- 进入审批节点时按 `timeout_seconds` 记录 deadline；后台线程周期扫描
  （`SWEEP_INTERVAL_SECONDS`，默认 5s），也可 `POST /admin/sweep` 手动触发。
- `escalate`：旧待办标记 `timeout_closed`，为 `to` 建新待办，只升级一次；
  `auto_approve` / `auto_reject`：系统身份推进或按 `on_reject` 拒绝。
- 扫描先锁实例（`SKIP LOCKED`），再锁活动/待办；单实例处理失败整笔回滚，
  下一轮自动重试，因此进程重启后会继续处理未完成升级。

## 目录结构

```
workflow_engine/
  main.py          FastAPI 入口、生命周期（种子 + 扫描线程）
  models.py        SQLAlchemy 模型（模板/版本/实例/活动/待办/审计/幂等台账）
  schemas.py       Pydantic 定义与请求/响应模型
  validator.py     发布前结构校验（引用/可达/环路/表达式）
  expressions.py   受限表达式求值器（AST 白名单）
  engine.py        运行时状态机（进入/推进/审批/拒绝/撤回）
  timeouts.py      超时扫描与升级（可重试、重启恢复）
  idempotency.py   request_id 幂等台账
  services/        模板版本服务、实例服务/视图
  api/             HTTP 路由
  sweeper.py       后台扫描线程
  demo.py          演示种子
alembic/           迁移
tests/             66 个测试（见下方清单）
demo/              演示流程 JSON
```

## 测试覆盖

```bash
pytest -q          # 需要 PostgreSQL；无可用 DB 时 db 用例自动 skip
```

- `test_validation.py` 表达式安全（注入/属性/下标/lambda 等拒绝）、缺失引用、
  不可达、环路、default 缺失、回边 start、超时配置一致性
- `test_versions.py` 发布校验、草稿发布、版本隔离、回滚只影响新实例、
  已发布版本不可变、expected_version 冲突
- `test_api.py` 全签/任签推进、明确拒绝关闭其余待办、**会签真并发竞争**、
  双拒绝竞争、幂等回放、冲突不占 request_id、越权（他人待办/跨实例 task_id）、
  仅提交人可撤回、条件路由
- `test_timeouts.py` 升级一次、auto_reject、失败重试、重启恢复、扫描与人工审批竞争
- `test_sweeper_and_demo.py` 后台线程真实升级、演示流程端到端、健康检查

## 范围外（按需求不做）

表单设计器、电子签名、多业务模块/多租户、消息队列、外部通知服务。
