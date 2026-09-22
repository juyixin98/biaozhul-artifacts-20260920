# CareOps 员工能力考核后端

用 Express + Sequelize + MySQL 实现的员工能力考核服务。**只覆盖组卷、评分与证书**；不含诊疗、陪诊订单或设备库存。

## 快速开始（Docker）

```bash
cp .env.example .env        # 可选，compose 已内置默认值
docker compose up --build   # 启动 MySQL 8 + 应用；自动迁移并播种演示数据
```

- 服务：http://localhost:3000 ，健康检查 `GET /health`
- 演示用户令牌（`Authorization: Bearer <token>`）：

| 角色 | 组织（时区） | 令牌 |
| --- | --- | --- |
| 学员 张学员 | 华东护理中心（Asia/Shanghai） | `token-stu-101` |
| 主管 李主管 | 华东护理中心 | `token-sup-102` |
| 管理员 | 华东护理中心 | `token-adm-103` |
| 学员 Anna | Berlin Hub（Europe/Berlin） | `token-stu-201` |
| 主管 Eva | Berlin Hub | `token-sup-202` |

本地无 Docker 时：`npm install` → 配置 MySQL（`.env`）→ `npm run migrate` → `npm run seed` → `npm start`。

## 规则与时间边界（实现要点）

### 题库与组卷
- 题型：`single` 单选、`multiple` 多选、`judge` 判断；难度 1–5。
- 每份试卷 **20 题**，其中难度 ≥4 的题 **至少 6 道（30% 向上取整）**。
- 组卷按种子确定性抽题（mulberry32 PRNG + Fisher–Yates）；**同 `orgId+seed` 返回同一份试卷**。
- 题库总数不足 20，或高难度题不足 6，接口 **明确返回 400 并说明缺口**，绝不重复抽题或降低标准凑数。
- 生成时把题干、选项、正确答案、解析、分值整体写入 `paper_questions` **冻结快照**；之后编辑题库只影响今后组卷，历史试卷不变。

### 考试与时间窗
- 每场限时 **30 分钟**：可作答窗口为 `[startedAt, deadlineAt)`，**`t == deadlineAt` 即超时**。
- 每人在**组织时区**（IANA，如 Asia/Shanghai）的自然日内最多 **开始 3 次**；开考接口返回当日日界（UTC 表示）。
- 并发开考通过 MySQL 命名锁 `GET_LOCK('start:u<id>')` 串行化计数，压测下也不会超过 3 次。
- 交卷必须带 `requestId`：
  - 同 `requestId` + 同内容（答案规范化 SHA-256）：返回原成绩（幂等）；
  - 同 `requestId` + 不同内容：**409 冲突**；
  - 场次已终态后换新 `requestId` 再交：409，终态不被覆盖。
- 超时（超过截止时间的交卷）按已提交内容判分，`arrival=late`、`finalReason=timeout`；一直未交卷由超时扫描（每分钟一次，或主管调 `POST /api/exams/sweep-timeouts`）按空答案判 0 分。
- 超时扫描与交卷并发：同一把场次命名锁 + 行锁 + 状态机（`in_progress → graded`），**只产生一个终态、一个首版成绩**。

### 评分与答案隔离
- 服务端评分，依据冻结快照。单选/判断须与答案一致；**多选必须选项集合完全一致，少选、多选均 0 分**。
- 换算百分制：`round(100 * earned / totalPoints, 2)`。
- 学员获取试卷/未出分成绩时**不含答案与解析**；出分后可看错题，错题明细保存**当时冻结的题干、选项与解析版本**。
- 成绩仅追加（`score_versions`），历史行不可修改；复核新增版本并记录原因、复核人。

### 掌握度
- 掌握度 = **最近一次有效（graded）考试**当前成绩版本的百分制分数；无有效考试为 `null`。
- 下降计时基准 `t0 = max(最近学习活动时刻, 最近出分时刻)`（交卷即记一次学习活动）。
- 当 `now - t0 >= 30 × 24h`（**满第 30 天的同一时刻**，29 天 23:59 仍不降）一次性 **−5 分，最低 0**；接口同时返回边界时间戳与文字说明。
- 主管可登记学习/培训活动，重置计时。

### 证书
- 发证双门槛（同时满足）：**当前掌握度 ≥ 85** 且 **当次有效考试成绩 ≥ 80**。
- 自发证起 **12 个自然月**有效（UTC 月运算）；`validUntil` 整时刻即过期（状态读取时即时判定，不修改记录）。
- **同一考试只发一张证书**（`session_id` 唯一）。
- 复核产生新版本后自动重算：成绩或掌握度跌破门槛则证书置 `revoked` 并记录原因。

### 授权
- Bearer Token 极简认证（演示用）；角色 `student / supervisor / admin`。
- 学员只能访问**自己的**考试、成绩、掌握度、证书；主管/管理员按**本组织**授权查询，跨组织不可见。

## 主要接口

| 方法/路径 | 角色 | 说明 |
| --- | --- | --- |
| `POST /api/papers` | 主管/管理员 | 按 `{seed,title}` 确定性组卷（不足明确失败） |
| `GET /api/papers/:id` | 已认证 | 学员视图（无答案） |
| `GET /api/papers/:id/full` | 主管/管理员 | 含冻结答案/解析 |
| `GET/POST/PATCH/DELETE /api/questions` | 主管/管理员 | 题库维护（删除为软停用，不影响快照） |
| `POST /api/papers/:paperId/exams` | 学员 | 开始考试（日限 3 次） |
| `POST /api/exams/:id/submit` | 学员 | 交卷 `{requestId, answers}`（幂等/冲突/超时） |
| `GET /api/exams/:id/result` | 学员本人/主管 | 成绩与错题；未出分不泄露答案 |
| `GET /api/exams` | 主管/管理员 | 按组织查询（可按 userId/status 过滤） |
| `POST /api/exams/:id/reviews` | 主管/管理员 | 复核 `{reason, adjustments:{order:得分}}`，新版本 |
| `POST /api/exams/sweep-timeouts` | 主管/管理员 | 立即推进所有超时场次到终态 |
| `GET /api/mastery` | 学员 | 自己的掌握度（含 30 天边界说明） |
| `GET /api/users/:id/mastery` | 主管/管理员 | 查学员掌握度 |
| `POST /api/users/:id/activities` | 主管/管理员 | 登记学习活动 |
| `POST /api/exams/:sessionId/certificate` | 学员 | 达标发证 |
| `GET /api/certificates` | 学员 | 我的证书（valid/expired/revoked 即时判定） |
| `GET /api/org/certificates` | 主管/管理员 | 本组织证书 |

## 数据库迁移

- 迁移文件：`src/db/migrations/*.js`（Umzug 驱动，元数据表 `migrations_meta`）。
- `npm run migrate`（up）；`node src/db/migrate.js status|down`。
- 表：`organizations, users, questions, papers, paper_questions, exam_sessions, submissions, score_versions, certificates, learning_activities`。

## 演示与测试

```bash
npm run demo     # 迁移+播种+端到端脚本：组卷→开考→满分交卷→掌握度→30天衰减→发证
npm test         # Jest + supertest，使用独立库 careops_test（需先建库，见下）
```

测试使用**可控时钟**（`src/config/clock.js`，可 `freeze/tick`），覆盖：
确定性组卷（同种子一致/异种子不同/题量不足失败/高难度不足失败/冻结不受题库编辑影响/答案隔离）、
并发开考次数限制与组织时区日界、30 分钟截止边界与超时扫描、交卷与超时并发终态唯一、
幂等评分与 requestId 冲突、服务端多选零分规则、错题冻结版本、复核 append-only、
掌握度 30 天边界、发证双门槛、12 个月有效期、同考不重发与复核降分吊销证书。

```sql
CREATE DATABASE careops_test CHARACTER SET utf8mb4; -- 测试前一次性创建
```

## 目录结构

```
src/
  config/        配置与可控时钟
  db/            Sequelize 连接、models、migrations、seed/demo
  services/      paper / grading / exam / review / mastery / certificate / activity
  controllers/   HTTP 控制器      middleware/   认证与错误处理
  routes/        路由装配         utils/        确定性随机、时区日界、答案哈希
tests/           4 个套件 28 个用例
```
