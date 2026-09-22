# StageVault — 演示会话后端

NestJS + TypeORM + PostgreSQL 实现的**演示会话**后端：不可变的演示文稿版本、有序场景、
加入码会话、主持人控制、基于递增事件序号的 WebSocket 同步，以及 30 分钟无活动自动结束。

> 仅演示版本 / 会话 / 同步，不含 3D 渲染或文件上传。

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后：

- API： http://localhost:3000
- 健康检查： `GET http://localhost:3000/health`
- Postgres： `localhost:5432`（库/用户/密码均为 `stagevault`）
- 容器启动时会自动等待数据库并执行迁移

本地开发（需要本机 Node 18+ 和一个 Postgres）：

```bash
npm install
export DATABASE_URL=postgres://stagevault:stagevault@localhost:5432/stagevault
npm run migrate
npm run start          # 构建并启动
# 或只跑数据库：docker run --rm -p 5432:5432 -e POSTGRES_USER=stagevault \
#   -e POSTGRES_PASSWORD=stagevault -e POSTGRES_DB=stagevault postgres:16-alpine
```

## 示例客户端

- 浏览器：直接用浏览器打开 [`examples/demo.html`](examples/demo.html)。
  一键“创建演示文稿并发布会话”，拿到 6 位加入码；其他人输入加入码和名字加入，
  可举手/取消，主持人可开始、暂停、继续、切场景、处理举手、结束。
- Node 脚本：[`examples/client.js`](examples/client.js)

```bash
node examples/client.js host                          # 主持人脚本化全流程
node examples/client.js participant ABC234 alice Alice # 参与者，stdin 输入 raise/cancel
```

## 测试

```bash
npm test
```

测试会自动在 Docker 里启动一个**一次性** Postgres（端口自动选择，结束即删除），
也可通过 `DATABASE_URL` 指定已有数据库。共 24 个用例，覆盖：

| 需求 | 测试文件 |
| --- | --- |
| 25 人上限的并发加入竞争、同人重复加入不占座 | `test/join-capacity.spec.ts` |
| requestId 幂等重试、同 ID 不同体冲突、expectedVersion 冲突 | `test/commands.spec.ts` |
| 状态机 lobby→live→paused→ended，结束不可恢复 | 同上 |
| 越权命令被拒且不推进序号 | 同上 |
| 举手按服务端顺序排队、去重、只能取消自己、主持人处理 | 同上 |
| 广播前“崩溃”（提交后广播抛错）事件仍持久可补齐 | `test/websocket-sync.spec.ts` |
| 断线按最后序号补齐；缺口/越界序号返回完整快照 | 同上 |
| 30 分钟超时自动结束、越权请求不续命、重启续跑超时、与主持人操作竞争边界 | `test/timeout.spec.ts` |
| 不可变版本、发布后再编辑不影响已发布版本、50 场景上限 | `test/presentations.spec.ts` |

## 领域模型

```
presentation 1──* scene                草稿（可随意增删改，最多 50 个有序场景）
             1──* presentation_version 发布快照（jsonb，不可变，版本号递增）

session       *──1 presentation_version 会话绑定“已发布版本”，后续草稿编辑不影响会话
   1──* participant                     主持人 + 参与者（唯一约束防重复/串行化防超额）
   1──* hand_raise                      举手队列（server_order 服务端接收顺序）
   1──* session_event                   状态事件日志（(session_id, seq) 唯一、连续）
   1──* command_record                  命令幂等记录（requestId + 请求体哈希 + 原始结果）
```

会话状态机：`lobby → live → paused → ended`（`live ⇄ paused` 可反复）。`ended` 终态不可逆。

## 关键设计

### 1. 发布即不可变，会话绑定版本

发布把当前草稿**拷贝**成一行 `presentation_versions.scenes`（jsonb 快照），版本号递增。
之后对 `scenes` 草稿表的替换式编辑不会触及任何已发布版本。创建会话时记录 `version_id`，
整个会话播放该快照。

### 2. 6 位加入码与 25 人上限

加入码取无歧义字母表（去掉 0/O/1/I/L），`char(6)` 唯一索引，冲突重试。
加入在一个事务里对 session 行加 `pessimistic_write` 行锁，再 `count` 参与者，
超员返回 `SESSION_FULL`；`(session_id,user_id)` 唯一索引兜底，因此并发加入在数据库层
就不可能超额。同一用户重复加入是幂等的，不占新座位。

### 3. 命令：requestId 幂等 + expectedVersion 乐观并发

`POST /sessions/:code/commands`，身份只从请求头 `x-user-id` 取（请求体不能伪造身份）：

```json
{ "requestId": "uuid/string", "expectedVersion": 3, "command": "changeScene", "sceneIndex": 2 }
```

- 同一 session 下同一 `requestId`：
  - 请求体哈希一致 → **直接返回首次执行时存下的原结果**（即使 expectedVersion 已过期，
    重试也不判冲突）；
  - 请求体不同 → `409 IDEMPOTENCY_CONFLICT`。
- 首次执行校验 `expectedVersion == last_event_seq`，不符 → `409 VERSION_CONFLICT`。
- 幂等查询与命令执行在**同一行锁事务**内：并发的“原请求 + 重试”会在锁上串行，
  后到者必然看到前一个已提交的 command_record 并重放。

命令类型：`start / pause / resume / end / changeScene`（仅主持人）；
`raiseHand / cancelHand`（参与者对自己）；`handleHand`（主持人，body 带 `targetUserId`）。

### 4. 状态变更与事件序号同事务，广播在提交之后

每次状态变化：

1. 在 session 行锁事务内把 `last_event_seq += 1`，写一行 `session_events`，
   更新 session 状态（同一事务提交）；
2. `(session_id, seq)` 唯一约束保证序号连续、重复/乱序写入会被数据库拒绝，状态无法回退；
3. **事务提交后**才通过 WebSocket 广播。广播失败（或进程在此时崩溃）不影响结果——
   事件已持久化，客户端靠补齐接口一定能拿到。广播只是加速器，不是事实来源。

### 5. 断线补齐与完整快照

WebSocket 连接携带 `lastSeq`：

- `lastSeq == 当前序号`：只入组，无历史；
- `lastSeq+1 … 当前序号` 的事件连续存在：只回放增量事件（`kind:"events"`）；
- 序号越界、或首个可用事件不等于 `lastSeq+1`（历史缺口）：返回**完整快照 + 当前序号**
  （`kind:"snapshot"`），客户端以快照为准重置。

连接后也可随时发 `catchup` 消息主动补齐。服务端对每个事件 seq 在客户端去重，
重复推送不会让本地状态回退。

### 6. 30 分钟无活动自动结束（含重启与竞争边界）

- session 行上持久化 `timeout_deadline = now + 30min`。**只有通过了鉴权和状态校验的
  合法命令 / 加入才会顺延**；越权请求在校验阶段就被拒绝，不会更新时间。
- `TimeoutScheduler` 周期性扫描 `deadline <= now()` 的会话；扫描本身无状态，
  截止时间全在数据库里，**服务重启后首次扫描即继续处理超时任务**。
- 与主持人命令的竞争边界由 session 行锁定义：
  - 主持人命令先拿到锁 → 顺延 deadline；扫描事务拿到锁后在锁内重新判断，
    deadline 在未来则什么都不做（操作优先）。
  - 扫描先拿到锁 → 置 `ended`、清空 deadline 并写终止事件；之后的主持人命令
    在锁内看到终态，返回 `SESSION_ENDED`，不能恢复。
- 自动结束产生 `session.ended` 事件，`payload.reason = "inactivity_timeout"`。
- 扫描间隔可用 `TIMEOUT_SWEEP_MS` 调整，超时时长用 `SESSION_IDLE_TIMEOUT_MS`
  （生产默认 1800000）。

### 7. 举手队列

- `hand_counter`（session 上的单调计数器）分配服务端顺序号，按**服务端接收顺序**排队；
  序号即使被处理/取消也不复用。
- `(session_id,user_id)` 唯一索引保证重复举手不会二次入队（返回幂等成功、不产生事件）。
- 参与者只能 `cancelHand` 自己的；`handleHand` 仅主持人可调用并把目标移出队列。

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/presentations` | 建演示文稿 + 草稿场景（≤50） |
| PUT | `/presentations/:id/scenes` | 替换草稿（仅 host） |
| POST | `/presentations/:id/publish` | 发布不可变新版本（仅 host） |
| GET | `/presentations/:id/versions/:v` | 读某个已发布版本 |
| POST | `/sessions` | 用已发布版本开会话，返回 6 位加入码 |
| POST | `/sessions/:code/join` | 加入（满员 409，同人幂等） |
| POST | `/sessions/:code/commands` | 命令（头 `x-user-id`，body 带 requestId/expectedVersion） |
| GET | `/sessions/:code/snapshot` | 当前完整快照 |
| GET | `/sessions/:code/events?afterSeq=N` | 增量事件；缺口时返回快照 |
| GET | `/health` | 健康检查 |

WebSocket：`GET /`（socket.io），握手 auth `{ sessionId(UUID或加入码), userId, lastSeq }`；
服务端事件：`connected` / `event` / `snapshot`；客户端消息：`catchup`。

## 迁移

TypeORM 迁移在应用启动时自动执行，也可单独运行：

```bash
npm run migrate
```

迁移文件：`src/migrations/1727000000000-initial-schema.ts`（应用内关闭了 `synchronize`，
只以迁移为准）。
