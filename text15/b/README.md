# ProofCycle — 包装打样审查后端

ProofCycle 是面向包装打样（packaging proofing）流程的审查后端：设计师提交本地 **PDF/PNG**
稿件，项目按 **设计师 + 项目经理(PM) + 最多 8 名指定审查员** 组织；每轮审查绑定
**不可变文件版本** 和 **检查清单快照**；PM 在所有指定审查员全部完成且无失败项时签核。

技术栈：**Go + Gin + GORM + MySQL 8**，提供 Docker / docker compose 一键启动。

> 明确不做：OCR、法规自动判定、仪表板。检查结论只能由审查员人工给出。

---

## 1. 快速开始（Docker）

```bash
docker compose up -d --build      # MySQL 8 + 应用，自动迁移并播种演示数据
# 应用：http://localhost:18090   MySQL：127.0.0.1:13306
curl http://localhost:18090/healthz
make demo                          # 或 ./scripts/demo.sh，跑完整业务流程
```

compose 启动时设置 `PROOFCYCLE_SEED_DEMO=1`，内置演示用户（固定 API token）和一个
带检查清单、已上传 v1 的演示作业：

| 角色 | 名称 | API token |
|---|---|---|
| 设计师 | demo-designer | `demo-designer-token` |
| 项目经理 | demo-pm | `demo-pm-token` |
| 审查员 1 | demo-reviewer-1 | `demo-reviewer-1-token` |
| 审查员 2 | demo-reviewer-2 | `demo-reviewer-2-token` |

停止：`docker compose down`（数据保留在卷里；`-v` 一并删除数据卷）。

### 本地直接运行

```bash
# 需要一个可连接的 MySQL 8；DSN 可用 PROOFCYCLE_MYSQL_DSN 覆盖
PROOFCYCLE_MYSQL_DSN='user:pass@tcp(127.0.0.1:3306)/proofcycle?charset=utf8mb4&parseTime=true&loc=UTC' \
PROOFCYCLE_STORAGE_DIR=./data/storage \
PROOFCYCLE_SEED_DEMO=1 go run ./cmd/proofcycle
```

所有配置通过环境变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PROOFCYCLE_HTTP_ADDR` | `:8080` | 监听地址 |
| `PROOFCYCLE_MYSQL_DSN` | 本机 proofcycle 库 | GORM/MySQL DSN |
| `PROOFCYCLE_STORAGE_DIR` | `./data/storage` | 落盘根目录（服务生成的路径全部在此之下） |
| `PROOFCYCLE_MAX_UPLOAD_MB` | `20` | 单文件大小上限（MiB） |
| `PROOFCYCLE_SEED_DEMO` | `0` | 为 `1/true` 时幂等播种演示数据 |
| `PROOFCYCLE_BOOTSTRAP_TOKEN` | 空 | 配置后允许用它调用 `POST /api/v1/users` 建用户；不配置则建用户接口关闭 |

---

## 2. 数据模型与核心规则

```
users (designer / pm / reviewer, 静态 API token)
  └─ jobs ──< job_reviewers            指定审查员（1..8，不可变）
      │      job_checklist_items       作业检查清单模板
      ├─< file_versions               不可变版本（唯一 (job, version)）
      └─< review_rounds ──< checklist_items   每轮绑定“版本 + 清单快照”
                              └─< opinions     (round, reviewer, item) 唯一
      signoffs                        签核审计记录（每轮至多一条）
```

关键不变量：

- **修订文件不可覆盖**：每次上传产生新 `version`，落盘到
  `files/job_<id>/v<n>/<随机名>.<ext>`，旧文件字节与 SHA-256 永久保留。
- **每轮绑定版本 + 清单快照**：新建轮次时复制清单模板，之后清单改动不改写历史。
- **检查项只有 `pass` / `fail` / `na`**；`fail` 必须填原因，否则 422。
- **存在未处理项或失败项不能批准**；签核接口返回 422，并在 `details` 中给出
  每个审查员未完成 / 失败的检查项代码。
- **新修订使旧轮次作废（superseded）**：旧意见在历史里完整保留，但永远不能作为
  新版本的批准依据；对已作废轮次提交意见返回 422 `version_superseded`。
- **审查员只能提交自己的意见**；设计师不能审批自己的作业（建作业时禁止 PM=设计师，
  签核处二次防御）。
- **意见更新携带 `expected_version`**（乐观锁，行级 `row_version`）；冲突返回 409。
- **最终签核条件**：作业处于 `in_review`、当前版本有 open 轮次、所有指定审查员对
  清单每一项均有非 fail 结论。

### 并发正确性

所有改动作业的事务在入口先 `SELECT ... FOR UPDATE` 锁住该作业行，因此
**并发签核、并发改意见、并发提交新修订** 被严格串行化：并发签核恰好一个成功
（另一个 409，`signoffs` 唯一约束兜底）；签核与“把某结论改成 fail”无论如何交错，
都不会出现“带 fail 却已批准”的状态。并发上传在锁内分配版本号，不产生版本号碰撞。

### 上传、校验与故障恢复

上传流程：先把文件流式写到 `tmp/<随机>.part`（在事务之外，慢 IO 不持锁），校验通过后
在**一个作业锁事务**内：置旧版本/轮次为 superseded → 将临时文件原子 `rename` 到最终
路径 → 插入版本/轮次/快照 → 推进 `current_version`。

- **大小**：流式 `LimitReader(上限+1)`，超限即拒；Gin 层先拦明显超大请求。
- **文件头**：只信任魔数（`%PDF-`、PNG 8 字节签名），不信任上传名 / Content-Type，
  扩展名由检测结果决定。
- **SHA-256**：落盘时计算并存库；下载和启动恢复时重新哈希校验，篡改/残缺文件
  不会被当作原件提供。
- **路径安全**：所有路径由服务生成；存储层对每个入参做“必须在根目录内”的规范化校验，
  `..`、绝对路径、跨目录 rename 一律拒绝；客户端文件名只做元数据，绝不拼进磁盘路径。
- **落盘/数据库失败可清理或恢复**：
  - 上传中断（客户端断开/取消）→ 立即删除临时文件，无任何版本产生；
  - 事务回滚 → 删除本次 rename 出去的文件；
  - 进程在“文件已 rename、事务未提交”窗口崩溃 → 启动 `RecoverOrphans` 清理无数据库
    引用的孤儿文件、清空 `tmp/`，并对全部在册文件做完整性校验（损坏直接报错而非静默服务）。

### 授权边界

每个作业的读取（详情、按版本历史、文件下载、报告）都先校验调用者是该作业的
设计师 / PM / 指定审查员之一；未授权返回 403，非参与者无法读取其他作业内容。
认证使用 `X-API-Token`（或 `Authorization: Bearer`），缺失/错误返回 401。

---

## 3. HTTP API

基址 `/api/v1`。除 `POST /users`（引导令牌）外均需 `X-API-Token`。

| 方法 & 路径 | 角色 | 说明 |
|---|---|---|
| `POST /users` | 引导令牌 | 建用户，返回一次性 `api_token` |
| `POST /jobs` | 设计师 | 建作业：`name`、`pm_id`、`reviewer_ids`(1..8)、`checklist[]` |
| `GET  /jobs` | 全部 | 仅列出与自己相关的作业 |
| `GET  /jobs/:id` | 参与者 | 作业详情 + 全部版本轮次（审查历史） |
| `POST /jobs/:id/revisions` | 该作业设计师 | `multipart/form-data`，字段 `file`（+可选 `file_name`），创建新版本与轮次 |
| `POST /jobs/:id/opinions` | 指定审查员 | 提交/更新某版本意见 |
| `POST /jobs/:id/signoff` | 该作业 PM | 最终签核 |
| `GET  /jobs/:id/versions/:v` | 参与者 | 按版本查询审查历史（清单快照+全部意见） |
| `GET  /jobs/:id/versions/:v/file` | 参与者 | 下载该版本原始文件（含 `X-SHA-256`） |
| `GET  /jobs/:id/versions/:v/report` | 参与者 | 报告；`?format=markdown` 或 `Accept: text/markdown` 导出 Markdown |

### 意见提交示例

```jsonc
POST /api/v1/jobs/1/opinions
{
  "version": 2,                       // 必须显式指定版本，杜绝写到新版本上
  "items": [
    {"checklist_code": "COLOR-01", "verdict": "fail",
     "reason": "Cyan Delta E 6.2, retarget required", "expected_version": 0},
    {"checklist_code": "DIM-02", "verdict": "pass", "expected_version": 0}
  ]
}
```

- 首次提交 `expected_version=0`；更新时传当前行版本（可在历史接口的
  `row_version` 字段取到），不匹配返回 **409 conflict**。
- `verdict` 非 `pass/fail/na` → 400；`fail` 无 `reason` → 422；
  对 superseded 轮次提交 → 422；非指定审查员 → 403。

### 签核被拦截的响应

```json
HTTP 422
{
  "code": "signoff_blocked",
  "message": "sign-off blocked by pending or failed checklist items",
  "details": {
    "reason": "all designated reviewers must complete every checklist item with no failures",
    "pending_by_reviewer": {"demo-reviewer-2": ["DIM-02", "BAR-04"]},
    "failed_by_reviewer":  {"demo-reviewer-2": ["COLOR-01"]}
  }
}
```

报告（Markdown）包含：作业/版本元信息、**文件摘要**（文件名、类型、大小、
SHA-256、上传时间）、**清单快照与逐条意见**（含失败原因）、**签核依据**
（每位指定审查员的 pass/na/fail/pending 计数与“零失败、零未决”声明），
未签核时列明未完成 / 失败项。

---

## 4. 数据库迁移

- 迁移文件：`internal/database/migrations/0001_init.sql`，使用 `//go:embed` 编入二进制；
- 启动时由 `database.Migrate` 按 `schema_migrations` 记录逐条、每文件一个事务执行，
  已应用则跳过（幂等）；
- 不依赖外键约束：引用完整性由服务在串行化事务内保证，崩溃清理也因此与删除顺序无关；
  状态/枚举由 MySQL `CHECK` 约束兜底。

---

## 5. 测试

```bash
# 先准备测试库（DSN 可用 PROOFCYCLE_TEST_DSN 覆盖）：
mysql -uroot -e "CREATE DATABASE IF NOT EXISTS proofcycle_test CHARACTER SET utf8mb4;
  CREATE USER IF NOT EXISTS 'proofcycle'@'%' IDENTIFIED BY 'proofcycle_pw';
  GRANT ALL ON proofcycle_test.* TO 'proofcycle'@'%';"

go test ./... -race -count=1
```

覆盖场景：

- **上传中断**：取消后无临时文件残留、无版本行；启动恢复无副作用；
- **版本失效**：v2 上传后 v1 轮次 superseded、对 v1 提交被拒、v1 完整意见不能批准 v2；
- **并发签核**：8 个并发签核恰好 1 个成功、`signoffs` 仅 1 行；签核与“改成 fail”
  并发多轮迭代，永不出现在有 fail 时被批准；并发上传版本号严格连续、文件不丢；
- **失败项拦截**：fail 必填原因；有 fail/有未决时 422 且明细准确；整改后放行；
- **文件越界**：存储层 `..`/绝对路径/外部 rename 拒绝，穿越文件名不会写到根目录外；
  孤儿文件 GC；在册文件被篡改后下载拒绝、恢复报错；
- **角色隔离**：审查员不能上传/签核、设计师不能签核、非指定审查员不能提交、
  未授权用户读不到作业/历史/报告/文件；无/错 token 401；
- 另有乐观锁版本冲突、大小上限、魔数嗅探、每作业 ≤8 审查员、非法 verdict、
  报告内容与授权等用例。

## 6. 目录结构

```
cmd/proofcycle/            入口：配置、迁移、恢复、播种、HTTP 服务、优雅退出
internal/config/           环境变量配置
internal/model/            GORM 模型与状态常量
internal/database/         迁移执行器 + 嵌入式 SQL
internal/storage/          文件落盘、校验、SHA-256、路径边界
internal/service/          全部业务规则与事务（作业锁）
internal/httpapi/          Gin 路由、中间件、处理器
internal/seed/             幂等演示数据
internal/tests/            MySQL 集成测试（HTTP 全链路 + 并发）
scripts/demo.sh            端到端演示
docker-compose.yml / Dockerfile
```
