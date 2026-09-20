# SkillPulse API 说明

所有接口除创建用户外，都需要请求头 `X-User-Id: <用户id>`。错误统一为：

```json
{ "error": { "code": "SOME_CODE", "message": "人类可读说明" } }
```

常用状态码：`400/422` 参数或业务前置不满足、`401` 未识别用户、`403` 角色或归属不符、`404` 不存在、`409` 状态冲突。

时间字段均为 ISO 8601 带时区；创建课程时若不带时区则按 UTC 处理。

---

## 1. 用户

### POST /users
创建主管或学员（无需身份头）。

```json
{ "email": "maya@example.com", "name": "Maya", "role": "MANAGER" }
```
`role`：`MANAGER` | `LEARNER`。返回 `{ id, email, name, role }`，后续用 `id` 作为 `X-User-Id`。

### GET /users/me
返回头中用户的信息。

---

## 2. 程序与版本（主管）

### POST /programs
```json
{ "title": "Onboarding", "description": "..." }
```

### POST /programs/{program_id}/versions
创建一个**草稿新版本**（版本号自动递增）。步骤按数组顺序获得 1..n 的序号，最多 50 个；`prerequisite_orders` 用序号引用前置步骤。

```json
{
  "steps": [
    { "title": "安全须知", "description": "观看视频", "pass_criteria": "测验≥80", "prerequisite_orders": [] },
    { "title": "实操演练", "description": "引导式演练", "pass_criteria": "讲师签字", "prerequisite_orders": [1] },
    { "title": "终审", "description": "主管复核", "pass_criteria": "通过", "prerequisite_orders": [1, 2] }
  ]
}
```

错误码：`STEPS_EMPTY`、`STEPS_TOO_MANY`、`PREREQUISITE_UNKNOWN`、`PREREQUISITE_SELF`、`STEPS_CYCLIC`。

### POST /versions/{version_id}/publish
发布草稿：再次运行依赖合法性与无环检查，冻结版本，并把程序的“当前版本”指针切到该版本。重复发布返回 `409 VERSION_ALREADY_PUBLISHED`。

### GET /versions/{version_id}
返回版本及步骤（含每步 `prerequisite_orders`）、`content_hash`（全部步骤内容的 SHA-256）、`status`。

### GET /programs/{id}/versions
列出程序全部版本（含未发布草稿）。

### PUT /programs/{program_id}/current-version
**回滚指针**：让后续新报名指向某个已发布旧版本。只改指针，不影响任何已报名学员的学习内容。

```json
{ "version_id": 3 }
```

> 已发布版本没有任何编辑接口；任何修改都必须新建版本。

---

## 3. 课程与报名

### POST /courses（主管）
一门课程恰好对应一个已发布版本。

```json
{ "version_id": 1, "capacity": 20, "enrollment_deadline": "2026-12-31T23:59:59+00:00" }
```

### GET /courses/{id}
```json
{ "id": 1, "version_id": 1, "capacity": 20, "enrollment_deadline": "...",
  "seats_taken": 12, "waiting_count": 3 }
```

### POST /courses/{course_id}/enroll（学员）
报名。行为：

- 超过报名截止：`422 ENROLLMENT_CLOSED`；
- 有空位：`status=ENROLLED`，分配 `seat_number`，`seat_expires_at = 现在+48h`；
- 无空位：`status=WAITLISTED`，分配 FIFO `waitlist_position`；
- 同一学员对同一版本重复报名：**幂等**返回原行，状态与座位不变；
- 取消/到期后再次报名会复用原报名行。

### POST /enrollments/{id}/confirm（学员）
48 小时内确认席位：`ENROLLED → CONFIRMED`，清除到期时间。重复确认幂等；席位已过期（已被候补顶上）返回 `409 SEAT_NOT_HELD`。

### POST /enrollments/{id}/cancel（学员）
取消。持有席位时释放并**立即按 FIFO 晋升**一名候补；候补取消则压缩队列位置。

### GET /enrollments/{id}（学员本人）
查看当前状态。

### POST /courses/{id}/expire-holds（主管）
立即执行到期扫描：释放所有超过 48 小时未确认的席位，并按报名顺序晋升候补。返回 `{ "expired": n }`。报名/确认/取消时也会自动触发同一逻辑，因此此接口主要用于定时任务或演示。

---

## 4. 学习进度与证书

### POST /enrollments/{id}/steps/{step_order}/submissions（学员本人）
```json
{ "content": "我的作业内容", "claimed_passed": true }
```

规则：

- 席位必须 `CONFIRMED`，否则 `422 SEAT_NOT_CONFIRMED`；
- 前置步骤未全部通过：`422 PREREQUISITES_NOT_PASSED`（返回缺失步骤序号）；
- 只能操作自己的报名；步骤序号必须属于报名时绑定的版本；
- 与上一次**已计数**提交（内容+声明）完全相同：幂等返回，`attempt_count` 不增加；
- 一旦主管把该步骤判为 FAILED，学员自行声明通过不再生效，须由主管纠正；
- 当全部步骤 PASSED 时自动签发证书；任一步骤不再 PASSED 时证书自动失效。

### POST /enrollments/{id}/steps/{step_order}/corrections（主管）
纠正结果，**原因必填**：

```json
{ "status": "FAILED", "reason": "作业存在抄袭" }
```

纠正后完成状态与证书立即同步：降级会使证书 `VALID → REVOKED`，重新通过且全部门通过时同一证书恢复为 `VALID`（序列号与内容摘要不变）。

### GET /enrollments/{id}/progress
返回每个步骤的状态、尝试次数、通过时间、纠正信息，以及合计与证书状态：

```json
{
  "enrollment_id": 7, "version_id": 1, "status": "CONFIRMED",
  "results": [ { "step_order": 1, "status": "PASSED", "attempt_count": 1, "...": "..." } ],
  "total_steps": 3, "passed_steps": 3,
  "certificate_id": 2, "certificate_status": "VALID", "certificate_serial": "SP-AB12..."
}
```

### GET /enrollments/{id}/certificate
返回唯一证书：`serial`（由 报名+版本 派生的唯一序列号）、`content_hash`（证书内容 SHA-256）、`status`、`issued_at`、`revoked_at`、`revoke_reason`。未全部通过时 `404`。重试任何操作都不会产生第二张证书。

---

## 5. 可控时钟（主管，演示/测试用）

| 方法/路径 | 主体 | 说明 |
| --- | --- | --- |
| GET /admin/clock | — | `{ now, virtual }` |
| PUT /admin/clock | `{ "now": "2026-01-10T09:00:00+00:00" }` | 设定虚拟时间 |
| POST /admin/clock/advance | `{ "minutes": 0, "hours": 48, "days": 0 }` | 推进虚拟时间 |
| POST /admin/clock/reset | — | 恢复真实时间 |

生产环境可设置 `ALLOW_CLOCK_CONTROL=0` 关闭写接口。

---

## 6. 端到端示例（约 2 分钟）

```bash
# 1) 用户
M=$(curl -s localhost:8000/users -H 'Content-Type: application/json' \
  -d '{"email":"m@example.com","name":"M","role":"MANAGER"}' | jq .id)
L=$(curl -s localhost:8000/users -H 'Content-Type: application/json' \
  -d '{"email":"l@example.com","name":"L","role":"LEARNER"}' | jq .id)

# 2) 程序 → 版本 → 发布
PID=$(curl -s -XPOST localhost:8000/programs -H "X-User-Id: $M" -H 'Content-Type: application/json' \
  -d '{"title":"Demo"}' | jq .id)
VID=$(curl -s -XPOST localhost:8000/programs/$PID/versions -H "X-User-Id: $M" \
  -H 'Content-Type: application/json' \
  -d '{"steps":[{"title":"S1","pass_criteria":"c","prerequisite_orders":[]},
                {"title":"S2","pass_criteria":"c","prerequisite_orders":[1]}]}' | jq .id)
curl -s -XPOST localhost:8000/versions/$VID/publish -H "X-User-Id: $M"

# 3) 课程 → 报名 → 确认
CID=$(curl -s -XPOST localhost:8000/courses -H "X-User-Id: $M" -H 'Content-Type: application/json' \
  -d "{\"version_id\":$VID,\"capacity\":2,\"enrollment_deadline\":\"2030-01-01T00:00:00Z\"}" | jq .id)
EID=$(curl -s -XPOST localhost:8000/courses/$CID/enroll -H "X-User-Id: $L" | jq .id)
curl -s -XPOST localhost:8000/enrollments/$EID/confirm -H "X-User-Id: $L"

# 4) 逐步提交（前置满足后才能提交 S2）
curl -s -XPOST localhost:8000/enrollments/$EID/steps/1/submissions -H "X-User-Id: $L" \
  -H 'Content-Type: application/json' -d '{"content":"done","claimed_passed":true}'
curl -s -XPOST localhost:8000/enrollments/$EID/steps/2/submissions -H "X-User-Id: $L" \
  -H 'Content-Type: application/json' -d '{"content":"done","claimed_passed":true}'

# 5) 证书
curl -s localhost:8000/enrollments/$EID/certificate -H "X-User-Id: $L"
```
