# API 说明

所有接口（除 `GET /healthz`）都需要请求头：

```
Authorization: Bearer <token>
Content-Type: application/json
```

响应统一为 JSON。错误形式：`{"error": "..."}`，状态码语义：

| 状态码 | 含义 |
|---|---|
| 400 | 参数错误 |
| 401 | 缺失/无效 token |
| 403 | 角色或层级无权、会员过期/取消、内容已下架 |
| 404 | 资源不存在（跨社区访问同样返回 404，避免存在性泄露） |
| 409 | 状态冲突（如审核竞争失败、request_id 冲突、恢复版本非最新） |
| 201/200/204 | 正常创建/读取/无内容成功 |

金额字段一律为整数分（`*_cents`）。时间为 RFC3339 UTC。

---

## 1. 社区与用户

### POST /admin/communities
用平台引导 token（环境变量 `PLATFORM_BOOTSTRAP_TOKEN`，未配置时本接口
不可用）创建新社区，调用者成为该社区管理员。
```json
{ "name": "围棋社区", "admin_name": "owner" }
```
→ 201 `{ "community": {...}, "admin": {...}, "admin_token": "tok_..." }`

引导 token 只能用于本接口；访问任何 `/communities/{cid}/...` 路由都返回
404。社区管理员用拿到的 `admin_token` 进行后续用户管理。

### POST /communities/{cid}/users  (admin)
```json
{ "username": "alice", "role": "member" }
```
role ∈ `admin|moderator|member`，默认 `member`。
→ 201 `{ "user": {...}, "token": "tok_..." }`；重名 → 409。

### GET /communities/{cid}/users  (admin)

---

## 2. 会员层级

### POST /communities/{cid}/tiers  (admin)
```json
{ "level": 3, "name": "Silver", "price_cents": 2990, "duration_days": 60 }
```
- `level` 1–10，社区内唯一；社区层级总数 > 10 → 409（触发器强制）。
- `price_cents ≥ 0`，`duration_days > 0`。

### GET /communities/{cid}/tiers
### POST /communities/{cid}/tiers/{id}/active  (admin)
```json
{ "is_active": false }
```
停用的层级不再授予访问权限（既有会员立刻失去该层级访问）。

---

## 3. 收款登记与会员有效期

### POST /communities/{cid}/payments  (admin；moderator 调用 → 403)

管理员确认线下收款后登记并续费：

```json
{
  "request_id": "pay-2026-0922-0001",
  "user_id": 12,
  "tier_id": 3,
  "amount_cents": 2990,
  "days": 60
}
```

规则：
- `request_id` 在社区内唯一，用于**幂等**。相同 `request_id` + 相同负载
  重放 → 200，返回当前订阅（不重复加时）。
- 相同 `request_id` + 任一字段不同 → **409**。
- 首单：创建订阅，`period_end = now() + days`。
- 续费（含已过期或已取消的订阅）：
  `period_end = greatest(period_end, now()) + days`，并把 status 置回 active。
- 并发首单通过 upsert 串行化为“一次插入 + 一次续费”，时长不丢失。

→ 200 `{ "subscription": { "status": "active", "period_end": "...", ... } }`

### GET /communities/{cid}/me/subscription
返回订阅与当前有效层级：`{ "subscription": {...}, "effective_level": 3, "access": true }`。
无订阅时 `subscription` 为 null、`access` 为 false。

### POST /communities/{cid}/me/cancel  (本人；admin 可带 `?user_id=`)
取消立即生效（status=cancelled），即使 `period_end` 尚未到达。
→ 204。

访问判定（所有读路径共用）：
`subscriptions.status='active' AND period_end > now() AND tiers.is_active`，
并要求 `tiers.level >= contents.required_level`。

---

## 4. 内容与版本

### POST /communities/{cid}/contents  (member/作者)
```json
{ "title": "定式讲解", "body": "正文...", "required_level": 3 }
```
创建 draft 内容与 v1 版本。→ 201 内容对象。

### GET /communities/{cid}/contents?status=
- moderator：全部。
- 其他：自己的内容（任意状态）+ 自己层级可读的 published 内容。

### GET /contents/{id}
作者/审核员返回元数据；会员在可读时返回 `{content, version}`（冻结发布版）。

### POST /contents/{id}/versions  (仅作者)
```json
{ "body": "新正文", "title": "可选新标题" }
```
追加不可变版本。若内容处于 pending，自动退回 draft，使在途批准无法发布新版本。
→ 201 `{ "content": {...}, "version": {...} }`。

### GET /contents/{id}/versions
作者/审核员：全部版本（倒序）。普通会员：仅冻结的已发布版本。

### GET /versions/{id}
按统一鉴权返回该版本；会员只能读到内容当前冻结的发布版本。

### POST /contents/{id}/submit  (作者)
```json
{ "version_id": 7 }
```
仅当 `version_id` 是当前最新版本时可提交；状态 pending。

### POST /contents/{id}/approve  (moderator，且不能是作者)
```json
{ "version_id": 7, "reason": "内容合规" }
```
原子条件：`status='pending' AND current_version_id=7`。
- 满足 → published，`published_version_id=7`，版本置 approved。
- 审核期间作者又编辑（当前版本变为 8，状态退回 draft）→ **409**。

### POST /contents/{id}/reject  (moderator)
```json
{ "version_id": 7, "reason": "需要补充来源" }
```
内容回 draft，版本置 rejected。

### POST /contents/{id}/delist  (moderator)
`{ "reason": "..." }` → published/pending 立即变 delisted，会员立刻不可读。

### POST /contents/{id}/restore  (moderator)
```json
{ "version_id": 7, "reason": "申诉成立，恢复" }
```
要求内容 delisted **且** `current_version_id = 7`（即恢复的是最新版本）。
下架后作者已更新出版本 8 时，恢复版本 7 → **409**，旧版无法覆盖新修改。

### GET /contents/{id}/events
内容审计流（submit/approve/reject/delist/restore，含操作者、版本、理由）。

---

## 5. 附件与导出

### POST /versions/{id}/attachments  (作者，仅当前可编辑版本)
```json
{ "filename": "notes.pdf", "content_type": "application/pdf",
  "data_base64": "<base64>" }
```
附件随版本冻结；已发布/被取代的版本不能再加附件。

### GET /versions/{id}/attachments
该版本附件元数据列表（同样需要该版本读权限）。

### GET /attachments/{id}
原始字节流（`Content-Disposition: attachment`）。与正文共用鉴权。

### GET /contents/{id}/export
返回 `application/zip`：`body.txt` + `attachments/*`，取调用者被授权的
冻结发布版本。正文、附件、导出三者鉴权完全一致。

---

## 6. 举报与申诉

### POST /contents/{id}/reports
```json
{ "version_id": 7, "category": "copyright", "reason": "疑似搬运" }
```
举报绑定具体版本。→ 201。

### GET /communities/{cid}/reports?status=
moderator 可见全部；作者可见针对自己内容的举报。

### GET /reports/{id} / GET /reports/{id}/events
举报详情与审计动作（每步含 `actor_id` 与 `basis`）。

### 裁决动作（moderator）

| 接口 | body | 状态转移 |
|---|---|---|
| POST /reports/{id}/accept | `{"basis":"已受理"}` | 受理（accepted 幂等） |
| POST /reports/{id}/uphold | `{"basis":"违规依据"}` | → upheld，内容**立即下架** |
| POST /reports/{id}/dismiss | `{"basis":"不违规依据"}` | → dismissed |
| POST /reports/{id}/appeal （仅作者） | `{"basis":"申诉依据"}` | upheld/dismissed → 待复核；**仅一次**，二次 → 409 |
| POST /reports/{id}/appeal/uphold | `{"basis":"复核维持依据"}` | → appeal_upheld |
| POST /reports/{id}/appeal/dismiss | `{"basis":"复核撤销依据"}` | → appeal_dismissed（之后可由 moderator restore） |

内容恢复仍走 `POST /contents/{id}/restore`，受“不能覆盖更新版本”保护。

---

## 7. 课程

### POST /communities/{cid}/courses
```json
{
  "title": "入门课",
  "required_level": 1,
  "modules": [
    { "position": 1, "title": "第一章",
      "lessons": [
        { "position": 1, "title": "课时1", "content_version_id": 7 }
      ] }
  ]
}
```
position 在同级必须唯一；每课时必须引用已存在的内容版本。

### PUT /courses/{id}/structure  (作者/moderator)
整体替换**草稿**结构。已发布快照不受任何影响。

### POST /courses/{id}/publish  (作者/moderator)
冻结草稿为不可变快照，并且**重新校验每一个课时**：
- 引用版本必须等于该内容当前的 `published_version_id`；
- 内容 status 必须是 published（草稿/下架 → 409）；
- 内容层级 ≤ 课程层级（否则 403）。

→ 201 `{ "publish": {...}, "structure": [...] }`。再次发布生成新快照，
旧快照保留。

### GET /courses/{id}
已发布：对非作者返回最新冻结快照；作者/审核员额外可见草稿。

### GET /courses/{id}/lessons/{position}
读取最新发布快照中的课时，随后执行与直接内容访问**完全相同**的鉴权
（会员有效性、层级、内容仍处于 published、版本即冻结版本）。
内容之后被下架时，课程内课时同样立即 403。
