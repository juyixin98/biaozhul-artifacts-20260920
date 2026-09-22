# StageVault — 演示会话后端（演示版）

NestJS + TypeORM + PostgreSQL 的演示会话后端：演示版本管理、会话生命周期、
WebSocket 状态同步。不含 3D 渲染与文件上传。

## 快速开始（Docker）

```bash
docker compose up --build
```

服务启动时会自动执行迁移，然后监听 `http://localhost:3000`（WebSocket 路径 `/ws`）。

本地开发：

```bash
npm install
createdb stagevault            # 或使用任意 PostgreSQL 实例，配置 DATABASE_URL
npm run migration:run
npm run start:dev
```

运行示例客户端（完整走一遍流程：建演示 → 发布 → 开会 → 加入 → 指令 → 举手 → 断线重连）：

```bash
node client/example-client.js
```

## 运行测试

测试需要 PostgreSQL（默认 `postgres://postgres:postgres@localhost:5432`，
会自动创建 `stagevault_test` 库，可用 `TEST_DATABASE_URL` 覆盖）：

```bash
docker compose up -d db        # 如果本机没有 PostgreSQL
npm test
```

覆盖：并发加入人数上限、指令幂等（含并发重复 requestId）、版本冲突、
广播前崩溃后事件仍可补齐、断线按序号补齐 / 缺历史回退快照、
超时与主持人指令竞争、越权不刷新活动时间、举手队列与去重。

## 核心设计

- **演示与版本**：一个演示最多 50 个有序场景（`scenes` 为有序 JSONB 数组，
  超过 50 拒绝）。版本发布后不可变（无更新接口，重复发布返回 409）。
  会话绑定某个**已发布**版本；之后再编辑只会产生新版本，不影响进行中的会话。
- **会话**：唯一 6 位加入码（无歧义字符集，冲突重试）。最多 25 名参与者
  （含主持人）：加入在事务内 `SELECT ... FOR UPDATE` 锁定会话行后计数，
  并发加入不会超额。状态机 `lobby → live ⇄ paused → ended`，`ended` 为终态。
- **指令**：仅主持人可发。每条指令带 `requestId`（幂等键）与
  `expectedVersion`（乐观并发）。同内容重试返回原结果（`replayed: true`）；
  同 `requestId` 不同内容返回 409；版本不匹配返回 409；结束后一切指令拒绝。
  状态变更、`version`/`event_seq` 自增、事件写入、回执写入在**同一事务**提交。
- **事件与同步**：每次状态变更在同一事务内追加 `session_events`（每会话
  单调递增 `seq`），提交后才广播——广播前崩溃不会丢事件，客户端重连后从
  事件表补齐。客户端按 `lastSeq` 订阅：缺历史（`lastSeq=0`、序号空洞或
  超前）时返回完整快照及当前序号；否则按序重放缺失事件。重复/乱序消息
  （`seq <= lastSeq`）在服务端与示例客户端两侧都被忽略，状态不会回退。
- **超时**：30 分钟无有效活动自动结束。扫描器每 15 秒运行，在事务内锁定
  会话行并复查 `(status != ended AND last_activity_at < cutoff)`：
  主持人指令先提交则活动时间已刷新、扫描跳过；扫描先提交则指令因
  `ended` 被拒绝。边界明确且基于持久化状态，服务重启后下一轮扫描继续处理。
  无权限/失败请求不会刷新 `last_activity_at`。
- **举手**：`hand_raises` 自增主键即服务端接收顺序（队列顺序）。部分唯一
  索引 `(session_id, participant_id) WHERE status='raised'` 保证重复举手
  不重复入队（返回已有记录 `duplicate: true`）。参与者只能取消自己的举手
  （按 token 定位本人），主持人处理后移出队列。

## API 概览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/presentations` | 创建演示 `{title}` |
| POST | `/presentations/:id/versions` | 创建版本 `{scenes: [{title, content?}]}`（≤50） |
| POST | `/presentation-versions/:id/publish` | 发布（之后不可变） |
| GET  | `/presentation-versions/:id` | 读取版本 |
| POST | `/sessions` | 创建会话 `{presentationVersionId, hostName}` → `{joinCode, hostToken}` |
| POST | `/sessions/join` | 加入 `{joinCode, name}` → `{token}` |
| POST | `/sessions/:id/commands` | 主持人指令 `{requestId, expectedVersion, type, sceneIndex?}`，`type ∈ start/pause/resume/goto_scene/end`，头 `x-session-token` |
| GET  | `/sessions/:id/state` | 完整快照（需 token） |
| GET  | `/sessions/:id/events?after=N` | 序号大于 N 的事件（需 token） |
| POST | `/sessions/:id/hand-raises` | 举手（需 token） |
| DELETE | `/sessions/:id/hand-raises/mine` | 取消自己的举手 |
| POST | `/sessions/:id/hand-raises/:participantId/resolve` | 主持人处理举手 |

### WebSocket 协议（`/ws`）

```jsonc
// 客户端首条消息
{ "type": "subscribe", "sessionId": "...", "token": "...", "lastSeq": 3 }
// 服务端
{ "type": "snapshot", "seq": 7, "state": { ... } }            // 缺历史时
{ "type": "event", "seq": 4, "eventType": "goto_scene", "payload": { ... } }
{ "type": "subscribed", "sessionId": "...", "seq": 7, "role": "participant", "participantId": "..." }
{ "type": "error", "code": "UNAUTHORIZED", "message": "..." }
```

## 配置

| 环境变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_URL` | `postgres://postgres:postgres@localhost:5432/stagevault` | 数据库连接 |
| `PORT` | `3000` | HTTP 端口 |
| `SESSION_TIMEOUT_MS` | `1800000` | 无活动自动结束阈值（30 分钟） |
| `SESSION_SWEEP_DISABLED` | `false` | 禁用周期扫描（测试用） |

## 目录结构

```
src/
  entities/          # TypeORM 实体
  migrations/        # 初始化迁移（含部分唯一索引）
  presentations/     # 演示与版本
  sessions/          # 会话、指令、举手、WS 网关、超时扫描
client/example-client.js
test/                # jest e2e 测试
```
