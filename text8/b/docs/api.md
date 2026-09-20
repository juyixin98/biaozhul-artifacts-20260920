# 接口文档

交互式文档：服务启动后访问 `/docs`（Swagger UI）或 `/redoc`。

所有写接口使用 JSON 请求体；身份通过请求头 `X-User` 传递（演示项目不做登录/签名）。
审批与撤回必须携带 `request_id`（幂等键）与 `expected_version`（实例绑定的模板版本号）。

错误响应统一格式：

```json
{"error": {"code": "version_conflict", "message": "..."}}
```

定义校验失败（422）额外带问题清单：

```json
{"error": {"code": "definition_invalid", "message": "...",
           "issues": [{"node_id": "x", "code": "cycle", "message": "..."}]}}
```

---

## 模板与版本

### POST /templates
创建模板；`publish:true`（默认）时校验并发布 v1，`false` 时创建草稿。

### GET /templates
列出模板。

### GET /templates/{key}
模板详情（含当前版本指针）。

### GET /templates/{key}/versions
全部版本（含草稿）。

### GET /templates/{key}/versions/{version}
版本详情（含不可变的完整 `definition`）。

### POST /templates/{key}/versions
用新定义创建版本（定义中的 `key` 必须与模板一致）；`publish:true` 时校验、发布并移动当前指针。

### POST /templates/{key}/versions/{version}/publish
发布已有草稿（执行完整发布前校验），并把当前指针指向它。

### POST /templates/{key}/rollback/{version}
把当前指针回滚到任一**已发布**版本（不存在返回 404）。只影响之后发起的实例。

---

## 实例

### POST /instances  （需 `X-User`，该用户即提交人）
```json
{"template_key": "expense", "version": 1, "title": "出差报销",
 "business_key": "BIZ-001", "context": {"amount": 300}}
```
`version` 省略时使用模板当前版本；只能基于已发布版本发起。
响应为实例详情（见 GET）。

### GET /instances/{id}
返回流程当前位置与完整视图：

```json
{
  "id": 1, "version_number": 1, "submitter": "tom",
  "status": "running",                 // running|approved|rejected|withdrawn
  "current_node_id": "manager",
  "context": {"amount": 300},
  "reject_reason": null,
  "pending_tasks": [
    {"id": 10, "node_id": "manager", "assignee": "alice",
     "status": "pending", "decided_at": null, "comment": null}
  ],
  "history": [
    {"id": 1, "event_type": "start", "node_id": "start",
     "actor": "tom", "actor_type": "submitter", "detail": {}, "request_id": null,
     "created_at": "2026-09-20T10:00:00+08:00"}
  ]
}
```

`status` 为 `rejected` 时 `reject_reason` 给出明确拒绝原因（或超时自动拒绝原因）。

### GET /instances/{id}/history
仅审计历史（按时间顺序）。事件类型：
`start, enter_approval, approve, reject, auto_approve, auto_reject, close_tasks,
escalate, condition_match, condition_default, complete, withdraw`。

### POST /instances/{id}/decide  （需 `X-User`）
query 参数二选一：`?task_id=10` 或 `?node_id=manager`。

```json
{"request_id": "req-001", "expected_version": 1,
 "action": "approve", "comment": "同意"}
```
`action` 为 `approve` 或 `reject`。

规则：
- 全签（`all`）：所有人通过才推进；**任一明确拒绝**即拒绝，关闭其余待办，按 `on_reject` 走；
- 任签（`any`）：一人通过即推进，其余待办关闭；一人拒绝即拒绝；
- 不是该待办的审批人、待办已处理、流程已结束、版本不符 → 409，**不写历史**；
- 相同 `request_id` 重放首次成功响应（`idempotent_replay: true`）。

响应：

```json
{"status": "running", "current_node_id": "manager", "advanced": false,
 "request_id": "req-001", "idempotent_replay": false}
```

### POST /instances/{id}/withdraw  （需 `X-User` = 提交人）
```json
{"request_id": "wd-001", "expected_version": 1, "comment": "信息填错"}
```
仅在流程未结束时由提交人撤回；关闭全部待办，状态变 `withdrawn`。非提交人 409。

---

## 管理 / 演示

### GET /health
`{"status": "ok"}`

### POST /admin/sweep
同步执行一轮超时升级扫描，返回 `{"processed": ["mgr:escalated"], "failed": []}`。
主要用于测试与演示；生产中由内置后台线程周期执行（`ENABLE_SWEEPER=true`）。

### POST /demo/seed
幂等创建并发布演示模板 `expense`（已存在则返回 `{"created": false}`）。

---

## 主要错误码

| code | 状态码 | 含义 |
| --- | --- | --- |
| `definition_invalid` | 422 | 发布校验失败（附 issues） |
| `template_key_exists` / `template_not_found` / `version_not_found` | 409/404 | 模板与版本 |
| `version_not_published` / `no_published_version` | 409 | 只能用已发布版本 |
| `version_already_published` | 409 | 草稿不能重复发布 |
| `version_conflict` | 409 | `expected_version` 与实例实际版本不一致 |
| `instance_not_found` / `instance_not_running` | 404/409 | 实例不存在或已结束 |
| `task_not_pending` | 409 | 无匹配待办（含非本人、已处理） |
| `not_assignee` / `task_instance_mismatch` | 409 | 越权操作他人待办 / 跨实例使用 task_id |
| `not_submitter` | 409 | 非提交人撤回 |
| `activity_not_pending` | 409 | 审批节点已被并发请求终结 |
| `condition_unmatched` | 409 | 条件全不匹配且无 default（发布期通常已拦截） |
