# SIRCC API

Base URL: `http://localhost:8080`（Docker Compose 映射到 `http://localhost:18082`）。

所有接口（除 `GET /healthz`）都要求请求头 `X-User-Id: <用户UUID>`。系统不连接任何外部身份或安全设备，用户使用迁移内置的种子账号：

| 用户 | UUID | 角色 |
|---|---|---|
| admin1 | `00000000-0000-0000-0000-0000000000a1` | admin（分配响应人员） |
| analyst1 | `00000000-0000-0000-0000-0000000000a2` | analyst（分诊、证据） |
| analyst2 | `00000000-0000-0000-0000-0000000000a3` | analyst |
| responder1 | `00000000-0000-0000-0000-0000000000b1` | responder（推进处置） |
| responder2 | `00000000-0000-0000-0000-0000000000b2` | responder |

## 生命周期

`detected → triaged → contained → eradicated → recovered → postmortem → closed`

- 每次只能前进一个阶段，不能跳阶段或回退。
- P1 事件必须先由 admin 分配响应人员（assignee）才能完成分诊。
- 关闭门槛：`rootCause`、`lessonsLearned` 非空，且至少一条带负责人和期限的行动项。
- 角色与案件范围：分诊（→triaged）仅 analyst；其余阶段仅当前 assignee（responder）可推进；证据仅 analyst；分配仅 admin。

## 错误格式

```json
{ "error": { "code": "version_conflict", "message": "..." } }
```

| HTTP | code | 含义 |
|---|---|---|
| 400 | `validation` | 请求体或参数非法 |
| 401 | `unauthenticated` | 缺少/未知 X-User-Id |
| 403 | `forbidden` | 角色或案件范围不允许 |
| 404 | `not_found` | 资源不存在 |
| 409 | `version_conflict` | expectedVersion 与当前版本不一致 |
| 409 | `invalid_transition` | 非法阶段跳转/资源状态不允许 |
| 422 | `triage_gate` | P1 未分配响应人员 |
| 422 | `close_gate` | 关闭前置条件未满足 |
| 422 | `evidence_limit` | 证据超过 50 条上限 |

## 接口

### 事件

- `POST /incidents` — 创建事件。Body: `{title, description, severity}`，severity ∈ P1..P4。返回 201 事件对象（初始 `status=detected, version=1`）。
- `GET /incidents` — 事件列表。
- `GET /incidents/{id}` — 事件详情。
- `POST /incidents/{id}/assign` — （admin）分配响应人员。Body: `{responderId}`。版本 +1。
- `POST /incidents/{id}/transitions` — 阶段转换。Body:
  ```json
  {"toStatus": "triaged", "expectedVersion": 1, "requestId": "client-uuid-1", "note": "可选"}
  ```
  - `expectedVersion` 必须等于当前版本，否则 409 `version_conflict`。
  - `requestId` 幂等：同一事件内重复提交返回首次结果（`replayed: true`），不重复推进。
  - 状态、阶段记录、审计事件在同一事务写入。
- `PUT /incidents/{id}/postmortem` — （assignee，仅 postmortem 阶段）写入 `{rootCause, lessonsLearned}`。
- `GET /incidents/{id}/audit` — 审计事件列表。
- `GET /incidents/{id}/export` — 导出：事件、阶段记录（含进入时间）、证据摘要（excerpt + 关联说明数）、行动项、指标。

### 证据（每案最多 50 条，提交后不可改）

- `POST /incidents/{id}/evidence` — （analyst）提交文本证据 `{content}`。并发提交在事件行锁下串行化，不会突破上限。
- `GET /incidents/{id}/evidence` — 证据列表。
- `POST /evidence/{evidenceId}/notes` — （analyst）追加关联说明 `{content}`（纠正只能追加，不能覆盖原证据）。

### 行动项与提醒

- `POST /incidents/{id}/action-items` — （assignee）创建 `{title, ownerId, dueAt}`（RFC3339）。
- `GET /incidents/{id}/action-items` — 列表。
- `POST /action-items/{id}/reschedule` — （assignee）改期 `{dueAt}`，`dueVersion` +1；旧版本的调度不会再发出过时提醒。
- `POST /action-items/{id}/complete` — （assignee）标记完成。

到期提醒由内置调度器（`REMINDER_INTERVAL`，默认 10s）持久化到 `reminders` 表；同一 `dueVersion` 只提醒一次，服务重启后首轮扫描自动补处理停机期间到期的行动项。

### 指标（export.metrics）

- `containmentDurationSeconds` = 进入 contained 时间 − 检测时间。
- `resolutionDurationSeconds` = 进入 closed 时间 − 检测时间。

只使用真实记录的阶段时间；阶段未完成时对应字段为 `null`，不伪造结束时间。
