# CareOps 员工能力考核后端

员工能力考核域：**组卷、考试评分、证书**。不包含诊疗、陪诊订单或设备库存。

技术栈：Node.js + Express + Sequelize + MySQL 8，Docker Compose 一键启动。

## 快速开始（Docker）

```bash
docker compose up -d --build
# 等待自动迁移与演示种子完成后：
curl http://localhost:3000/health
```

容器启动时会自动：等待 MySQL → 执行迁移 → （`ENABLE_DEMO_SEED=true` 时）写入演示数据 → 启动 API。

演示账号（`x-user-id` 头鉴权，见下文）：

| 角色 | 用户 | 组织 | 说明 |
| --- | --- | --- | --- |
| admin | 1 | — | 平台管理员 |
| supervisor | 2 | 华北护理示范中心 (Asia/Shanghai) | 主管 |
| supervisor | 3 | 华东康复医院 | 他组织主管（用于越权演示） |
| student | 4 / 5 | 华北护理示范中心 | 学员张三 / 李四 |
| student | 6 | 华东康复医院 | 学员王五 |

跑端到端演示脚本（组卷→开考→幂等交卷→发证→复核吊销→授权校验，共 23 项断言）：

```bash
npm run demo
```

## 本地开发（无 Docker）

需要一个可连接的 MySQL 8。

```bash
cp .env.example .env          # 按需修改 DB_* / PORT
npm install
npm run migrate               # forward-only 迁移（schema_migrations 记录）
npm run seed:demo             # 可选：演示题库/用户/一张确定性试卷
npm start                     # http://localhost:3000
```

## 测试

```bash
DB_HOST=127.0.0.1 DB_PORT=3306 DB_USER=root DB_PASSWORD=careops npm test
```

`npm test` 自动创建 `careops_test` 库并迁移，然后用 `--runInBand` 串行运行 30 个集成测试。

**可控时钟**：服务端所有时间判断都读取请求时间 `req.now`。当环境变量
`ALLOW_CLOCK_OVERRIDE=1` 时，请求可带 `x-now: <ISO-8601>` 头注入时间，
用于精确测试时区边界与到期。生产配置默认关闭，该头会被忽略。

测试覆盖（对应需求逐条）：

| 测试文件 | 覆盖点 |
| --- | --- |
| `tests/paper.test.js` | 确定性组卷（同种子同试卷）、20 题且 ≥30% 难度≥4、题量/难题不足明确失败且不重复凑数、组卷后题库编辑不影响历史试卷 |
| `tests/exam-concurrency.test.js` | 组织时区每日 3 次上限、6 并发开考只有 3 个成功、跨上海午夜重置、到点/迟到 1ms 边界、超时终态唯一且幂等、超时与提交并发只产生一个终态、同请求 ID 同内容重放/不同内容 409、4 并发重复交卷只评一次分 |
| `tests/grading.test.js` | 提交前答题视图无答案/解析、学生无法访问题库答案、多选少选/多选均 0 分（完全一致才得分）、判断精确判分、错题存快照版本、迟到 0 分、考试期间题库改答案不影响判分 |
| `tests/certificate.test.js` | 掌握度≥85 且考试≥80 才发证、12 个月有效期边界（前 1ms 有效、到点过期）、同考试不重复发证、复核降分追加新版本并吊销证书、历史版本不可改、恢复后证书同行恢复、30 天无学习 -5（29/30/59/60 天边界）、最低 0、过期考试不算学习、学员仅看自己、主管按组织授权、越权复核 403 |

## 业务规则与时间边界

### 组卷与冻结

- 题型：`single` 单选、`multiple` 多选、`boolean` 判断；难度 1–5。
- 每张卷 20 题，至少 `ceil(20 * 0.3) = 6` 题难度 ≥ 4。
- 组卷由 `selectionSeed` 决定（确定性 RNG：splitmix32 + mulberry32 + Fisher–Yates）。
  题库输入按 id 排序，同题库状态 + 同种子必然产出同一张卷。
- 题量不足 20 或难度 ≥4 的题不足 6 时返回 `422 INSUFFICIENT_QUESTIONS` /
  `422 INSUFFICIENT_HARD_QUESTIONS`，**绝不重复题目凑数**。
- 组卷时把题干、选项、正确答案、解析、单题分值与评分规则复制为不可变快照
  （`paper_questions`）。此后判分与错题复习只读卡快照行；题库增删改不触及历史试卷。

### 开考与次数

- 考试时长 30 分钟（`deadline_at = started_at + 30min`）。
- 每人每个**组织本地日**最多开始 3 次（按组织时区 IANA 名计算 `YYYY-MM-DD`）。
- 开考在 SERIALIZABLE 事务内锁用户行 + 锁/建当日计数行后自增；并发开考不能突破上限。
  次日按组织时区午夜重置。

### 交卷、幂等与超时

- 交卷必须带请求 ID（`X-Request-Id` 头或 `body.requestId`），它是幂等键：
  - 同 ID + 同内容（规范化 JSON 的 sha256）→ 返回原成绩，不重新评分；
  - 同 ID + 不同内容 → `409 IDEMPOTENCY_CONFLICT`。
- 交卷事务先锁考试行，任何并发提交/超时都在此排队；终态
  （`submitted` / `expired`）只产生一个，成绩版本只产生一条。
- 时间边界：`now <= deadline` 交卷正常评分；`now > deadline`（晚 1ms 即算）记 0 分、
  状态 `expired`，答案仍留档审计。超时接口在到达 `deadline` 时刻即可终结，且幂等。
- 另有管理员接口 `POST /api/admin/sweep-expired` 批量终结到期考试（同保证）。

### 评分

- 服务端判分，按冻结快照。评分规则 `exact`：
  单选/判断须唯一匹配；多选的选项集合须与答案**完全一致**，少选、错选、多选均 0 分，无部分分。
- 每题 5 分，百分制按 `earned / max * 100` 四舍五入。
- 答题视图（开考后/考试中）不含 `answer`、`explanation`；这些只在终态后的结果里出现。
- 错题记录引用 `paper_questions` 快照（含版本号），展示当时的题干/答案/解析。

### 成绩版本与复核

- 成绩只追加（`score_versions`）：自动评分是 v1；每次复核追加 v2、v3…，旧行不可修改。
- 复核必须填写原因（审计）；学员不能复核，主管只能复核本组织。
- 考试的当前成绩指向最高版本；掌握度与展示都读当前版本。

### 掌握度

- 掌握度 = 最近一次**有效考试**（实际提交、当前版本）的百分制成绩。
- 自最近一次有效考试起，每经过完整 30 个自然日无新的有效考试，减 5 分：
  - 第 29 天 23:59:59.999 不减；满 30×24h 那一刻减 5；第 59 天仍 -5；第 60 天 -10；
  - 下限 0；新有效考试重置计时；**过期（未提交）考试不算学习活动**。

### 证书

- 发证条件（交卷当时同时满足）：掌握度 ≥ 85 且本次成绩 ≥ 80。
- 有效期 12 个自然月：`now < validUntil` 有效；到达 `validUntil` 那一刻即过期（读时派生）。
- 同一考试最多一张证书（唯一约束），重考是新考试、不会给旧考试补发。
- 复核把成绩降到 80 以下 → 证书立即 `revoked` 并记录原因；之后复核恢复到 ≥80，
  同一证书行恢复有效。复核升分不追补发证。

## API 摘要

所有业务接口前缀 `/api`，用 `x-user-id: <id>` 表示当前用户（教学用简化鉴权）。

| 方法 | 路径 | 角色 | 说明 |
| --- | --- | --- | --- |
| POST | `/questions` | 主管/管理员 | 新建题目（难度1-5、选项/答案校验） |
| PUT | `/questions/:id` | 主管/管理员 | 改题库（仅影响后续新卷） |
| GET | `/questions` | 主管/管理员 | 题库列表（含答案） |
| POST | `/papers` | 主管/管理员 | 组卷，body：`{seed?, title?, count?, minHardRatio?}` |
| GET | `/papers/:id` | 主管/管理员 | 试卷（含冻结答案） |
| POST | `/exams` | 学员 | 开考 `{paperId}`；超限 409 |
| GET | `/exams/:id` | 本人/管理员 | 答题视图（无答案） |
| POST | `/exams/:id/submit` | 本人 | 交卷，`X-Request-Id` + `{answers}` |
| POST | `/exams/:id/timeout` | 本人/管理员 | 到期终结（幂等） |
| GET | `/exams/:id/result` | 本人/本组织主管/管理员 | 终态成绩与逐题解析 |
| GET | `/exams/:id/history` | 同上 | 成绩版本链（v1 + 各复核版） |
| POST | `/exams/:id/review` | 本组织主管/管理员 | 复核 `{score:0-100, reason}` |
| GET | `/me/exams` | 学员 | 我的考试 |
| GET | `/me/mastery` | 学员 | 当前掌握度及衰减明细 |
| GET | `/me/certificates` | 学员 | 我的证书（含派生状态） |
| GET | `/me/wrong-questions` | 学员 | 我的错题（快照内容） |
| GET | `/exams?userId=&status=` | 主管/管理员 | 按组织授权查询 |
| GET | `/users/:id/mastery` | 本组织主管/管理员 | 查看学员掌握度 |
| POST | `/admin/sweep-expired` | 管理员 | 批量终结到期考试 |

`answers` 支持按题号位置提交：`{"1": ["A","C"], "2": ["true"], "3": "B"}`
（值可为单字符串或数组；也支持数组形式 `[{position:1, answer:[...]}]` 与
`question:<paperQuestionId>` 键）。

## 数据模型

```
organizations ─┬─ users (student/supervisor/admin)
               ├─ questions (live bank)
               └─ papers ─ paper_questions (frozen snapshot: stem/options/answer/explanation/rule)
exams ─ daily_attempt_counts      (user_id, org-local day_key) 唯一
exams ─ score_versions            (exam_id, version) 唯一，append-only
exams ─ submissions               (exam_id, request_id) 唯一 + content_hash 幂等
exams ─ wrong_questions → paper_questions
certificates                      exam_id 唯一（同考试不重复发证）
schema_migrations                 forward-only 迁移记录
```

## 目录结构

```
src/
  config/            规则常量（时长/上限/阈值/有效期）单一来源
  db/                连接、迁移运行器、migrations/*.sql
  models/            Sequelize 模型与关联
  services/          paperService / grading / examService
                     masteryService / certificateService / wrongQuestionService
  middleware/        可控时钟、简化鉴权、错误处理
  routes/            bank(questions+papers) / exams
  seeders/           演示数据
tests/               4 个套件 30 个测试 + helpers
scripts/             demo.js 端到端演示、测试建库
docker-compose.yml / Dockerfile / docker-entrypoint.sh
```
