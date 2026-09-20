# ProofCycle — 包装打样审查后端

Go + Gin + GORM + MySQL 实现的打样审查服务。仅支持本地 PDF/PNG 文件，
不包含 OCR、法规自动判定与仪表板。

## 快速启动（Docker）

```bash
docker compose up --build -d   # MySQL 8 + 应用，自动迁移并写入演示用户
./scripts/demo.sh              # 端到端演示：建作业→上传→审查→签核→导出报告
```

演示用户（`SEED_DEMO=true` 时写入）：`alice`(项目经理,id=1)、`bob`(设计师,id=2)、
`carol`~`judy`(审查员,id=3~10)。身份通过请求头 `X-User-ID` 识别。

本地开发：`go test ./...`（测试使用内存 SQLite，无需 MySQL）。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `ADDR` | `:8080` | 监听地址 |
| `DB_DSN` | 本地 MySQL | MySQL DSN |
| `STORAGE_ROOT` | `./data/files` | 文件存储根目录 |
| `MAX_UPLOAD_MB` | `50` | 单文件大小上限 |
| `SEED_DEMO` | `false` | 是否写入演示用户 |

## API 一览（除 `/healthz` 外均需 `X-User-ID`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/jobs` | 项目经理创建作业（设计师 + ≤8 名审查员 + 检查清单模板） |
| GET | `/jobs/:id` | 作业详情（仅参与者） |
| POST | `/jobs/:id/revisions` | 设计师上传新修订（multipart 字段 `file`） |
| GET | `/jobs/:id/revisions` | 版本列表 |
| GET | `/jobs/:id/revisions/:vid/history` | 按版本查询审查历史 |
| GET | `/jobs/:id/revisions/:vid/file` | 下载版本文件 |
| PUT | `/jobs/:id/revisions/:vid/checklist/:itemId` | 更新检查项 `{status, fail_reason, expected_version}` |
| POST | `/jobs/:id/revisions/:vid/comments` | 审查员发表意见 `{content}` |
| PUT | `/comments/:id` | 作者修改意见 `{content, expected_version}` |
| POST | `/jobs/:id/revisions/:vid/complete` | 审查员标记完成本轮审查 |
| POST | `/jobs/:id/approve` | 项目经理最终签核 |
| GET | `/jobs/:id/report?revision_id=` | 导出报告（文件摘要、清单快照、签核依据） |

## 核心规则

- **文件**：仅 PDF/PNG（按文件头魔数判定）；限制大小；流式计算 SHA-256；
  存储路径由服务端生成（`作业ID/版本ID-UUID.扩展名`），客户端文件名仅作展示；
  临时文件 + fsync + 原子 rename，上传中断不留残余；数据库失败会补偿删除已落盘文件；
  版本文件不可覆盖。
- **审查轮次**：每次修订绑定文件版本与检查清单快照；检查项仅 `pass`/`fail`/`na`，
  `fail` 必须填原因；存在未处理或失败项时不能签核。
- **版本失效**：新修订提交后旧版本意见保留可查，但一切写操作被拒绝，
  且不能作为新版本的签核依据（新版本的清单与审查状态全部重置）。
- **权限**：审查员只能提交/修改自己的意见；设计师不能批准自己的作业；
  非参与者对作业、历史、文件、报告一律 403。
- **并发**：作业级守卫更新（条件原子 UPDATE）串行化同一作业的并发写，
  并发签核仅一个成功；意见与检查项使用 `expected_version` 乐观锁，冲突返回 409。
- **签核**：要求所有指定审查员完成当前版本审查且当前版本无未处理/失败项；
  签核记录保存文件 SHA-256 作为依据；签核后作业冻结。

## 迁移与测试

- 迁移为版本化 Go 迁移（`internal/migrate`），启动时自动应用并记录于 `schema_migrations`。
- 测试覆盖：上传中断清理、类型/超限校验、路径越界、版本失效、失败项拦截、
  审查员完成度、并发签核、乐观锁冲突、角色隔离、报告内容与鉴权。
