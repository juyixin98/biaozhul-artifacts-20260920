# ForensicCore — 本地证据处理后端

ForensicCore 是一个用 **Go + Gin + GORM + MySQL** 实现的本地数字证据处理后端，
提供 Docker 一键启动。它只接收 **raw / dd 原始磁盘镜像**，负责：

- 案件登记、镜像**基线登记**（大小 + SHA-256，只读分块计算）；
- 持久化、可中断恢复的**完整性复核作业**（带进度与错误）；
- **只追加（append-only）的哈希证据链**：登记 / 复核 / 移交 / 备注，带案件内序号、
  前一摘要与规范化内容摘要，可检测缺失、篡改与乱序；
- 两种角色：**调查员（investigator）**可登记与移交，**分析师（analyst）**只能查询与备注；
- 导出包含基线、复核结果、完整证据链与免责声明的案件报告。

**明确不做的事**：不解析磁盘/文件系统、不做删除恢复、不做司法合规认证。

> ⚠️ **哈希链的边界**：哈希链只能提供**完整性检查**（发现链上事件的缺失、篡改、乱序），
> 它**不能证明事件发生的真实时间**（系统内没有可信时间戳），也**不能代替外部数字签名**
> 或司法鉴定/合规认证。该声明同时写入每一份导出报告（`disclaimer` 字段）。

---

## 1. 快速开始（Docker）

前置：Docker 24+ 与 Docker Compose v2。

```bash
cp .env.example .env
# 编辑 .env，至少修改 INVESTIGATOR_TOKEN 与 ANALYST_TOKEN
docker compose up -d --build
curl -s http://localhost:8080/healthz
```

首次启动时：

1. MySQL 8.4 容器完成初始化与健康检查；
2. API 容器在 `/data/samples` 内生成两个小镜像样例（`sample_1mb.dd`、`sample_256k.raw`）；
3. GORM 自动建表。

你的真实镜像目录通过只读挂载暴露给容器，例如把宿主机 `/home/me/images` 挂到
`/data/evidence`（在 `.env` 设置 `EVIDENCE_HOST_DIR=/home/me/images`）。compose
默认以 **`:ro` 只读**挂载该目录，应用自身也始终以只读方式打开镜像。

镜像内可登记的目录由 `WHITELIST_DIRS` 限定（默认 `/data/samples:/data/evidence`）。

### 本地直接运行（无需 Docker，使用 sqlite）

测试与本地开发使用纯 Go 的 SQLite 驱动，**不需要 CGO**：

```bash
make seed          # 生成 ./samples 下的样例
make run           # sqlite + ./samples 白名单，监听 :8080
make test          # 运行全部测试
make race          # 带 -race 运行测试
```

---

## 2. 认证与权限

所有 `/api/v1/**` 接口使用 Bearer Token：

```
Authorization: Bearer <token>
```

| 能力 | 调查员 investigator | 分析师 analyst |
|---|---|---|
| 建案件 / 登记镜像 / 发起与恢复复核 / 移交 | ✅ | ❌（403） |
| 查询案件、证据、复核、证据链、报告 | ✅ | ✅ |
| 添加备注 note | ✅ | ✅ |
| 无 token / 错误 token | 401 | 401 |

操作人（`actor`）记录为 `role:investigator` / `role:analyst` 并进入链上内容摘要。

---

## 3. HTTP API 速览

案件：

```
POST /api/v1/cases                 {"id":"可选自定义ID","name":"…","description":"…"}
GET  /api/v1/cases
GET  /api/v1/cases/:id
```

证据（raw/dd，路径必须在白名单目录内）：

```
POST /api/v1/cases/:id/evidence    {"source_path":"/data/samples/sample_1mb.dd"}
GET  /api/v1/cases/:id/evidence
GET  /api/v1/evidence/:eid
POST /api/v1/evidence/:eid/transfer   {"to_custodian":"lab-7","reason":"…"}
POST /api/v1/cases/:id/notes          {"evidence_id":"可选","note":"…"}
```

复核作业（异步、持久化、可恢复）：

```
POST /api/v1/evidence/:eid/reviews      # 启动复核
GET  /api/v1/cases/:id/reviews
GET  /api/v1/reviews/:rid
POST /api/v1/reviews/:rid/resume        # 恢复；先验证身份+已处理前缀
POST /api/v1/reviews/:rid/cancel        # 在最近检查点暂停
```

证据链与导出：

```
GET  /api/v1/cases/:id/chain           # 完整链（按 seq）
GET  /api/v1/cases/:id/verify          # 完整性校验报告（缺失/篡改/乱序）
GET  /api/v1/cases/:id/report          # 基线 + 复核 + 完整链 + 校验 + 免责声明
```

### 一次端到端演练

```bash
INV="Authorization: Bearer $INVESTIGATOR_TOKEN"
ANA="Authorization: Bearer $ANALYST_TOKEN"

CID=$(curl -s -H "$INV" -H 'Content-Type: application/json' \
  -d '{"name":"2026-09-case"}' http://localhost:8080/api/v1/cases | jq -r .case.id)

curl -s -H "$INV" -H 'Content-Type: application/json' \
  -d '{"source_path":"/data/samples/sample_1mb.dd"}' \
  http://localhost:8080/api/v1/cases/$CID/evidence | jq .

curl -s -H "$INV" http://localhost:8080/api/v1/cases/$CID/verify | jq .
curl -s -H "$ANA" http://localhost:8080/api/v1/cases/$CID/report | jq .disclaimer
```

---

## 4. 关键安全设计

### 4.1 基线登记：只读、分块、两次哈希、变化即失败

- 仅接受扩展名 `.raw` / `.dd`；其他类型、目录、设备、套接字一律拒绝。
- 打开前：`filepath.Abs` → `filepath.EvalSymlinks`（解析**所有**中间目录与末端符号链接）
  → 校验真实路径必须位于某个白名单根之内（分隔符感知，`/data/v` 不会匹配 `/data/va`）。
- 打开时：Linux 使用 `O_RDONLY|O_NOFOLLOW|O_CLOEXEC`，打开后对**文件描述符** `fstat`，
  记录 `dev/inode/size/mtime`，并再次确认真实路径仍在白名单内，闭合“检查-打开”TOCTOU 窗口。
- 分块（默认 4 MiB）流式 SHA-256，**全程不写镜像**。
- **连续两遍**哈希；每遍前后都比对 `dev/inode/size/mtime` 身份。任何不一致
  （包括同尺寸改写、截断、增长、路径处替换 inode）都返回 `file changed while being read`，
  且**不会写入任何基线记录或链事件**——错误基线无法被保存。

### 4.2 完整性复核：持久化作业 + 可验证的恢复

复核是数据库中的持久化作业（`review_jobs`），周期保存：

```
offset（已处理字节）、prefix_sha256（已处理部分的 SHA-256）、
status、final_sha256、result(match/mismatch)、error_message、时间戳
```

- 中断（取消 / 进程重启）后状态为 `interrupted`，重启时自动扫描并尝试续算。
- **续算前不只看文件名或大小**，而是：
  1. 重新走白名单解析与只读打开，比较 **真实路径 + device/inode + size**；
  2. 对 `[0, offset)` 已处理部分重新计算 SHA-256，必须与保存的 `prefix_sha256` 完全一致；
  3. 任一不符（前缀被改写、同路径换了 inode、尺寸变化、符号链接逃逸）→ 作业置 `failed`，
     绝不续算不可信内容。
- 复核结束再次核对文件身份，并将最终摘要与登记基线比对，结论 `match` / `mismatch`
  作为 `review` 事件追加到链上。

### 4.3 只追加哈希链

- 每个案件有创世摘要 `SHA256("forensiccore-genesis-v1:" + caseID)`。
- 事件内容先序列化为**规范化 JSON**（时间统一 UTC/RFC3339Nano；载荷递归按键名排序，
  规范化版本号 `v=1`），再计算：

  ```
  digest_n = SHA256( digest_(n-1) || canonical_content_n )
  ```

- 数据库对 `(case_id, seq)` 建唯一索引；服务内按案件加互斥锁，跨进程则由唯一约束兜底，
  并发追加在冲突时有界重试。**链不会分叉，也不会丢事件。**
- `GET /verify` 从创世摘要开始重走全链，输出每一处：
  - `gap_in_sequence`（缺失/删除）、
  - `broken_prev_link`（乱序/替换/重指向前驱）、
  - `content_digest_mismatch`（内容或摘要被篡改）、
  - 空链、异常序号等问题。

> 再次强调：这是**完整性**机制，不是可信时间源，也不是外部签名。

### 4.4 证据不被修改

- 镜像以只读方式打开；证据挂载点在 compose 中就是 `:ro`。
- 移交只更新“当前保管人”字段并追加链事件；备注只追加链事件；二者都不触碰镜像内容。

---

## 5. 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HTTP_LISTEN` | `:8080` | 监听地址 |
| `DB_DRIVER` | `mysql` | `mysql` 或 `sqlite`（后者仅用于本地/测试） |
| `DB_DSN` | 指向 compose 中的 mysql | GORM DSN |
| `WHITELIST_DIRS` | `/data/samples` | `:` 分隔的白名单根，启动时解析为真实路径 |
| `HASH_CHUNK_SIZE` | `4194304` | 分块哈希字节数（≥4096） |
| `INVESTIGATOR_TOKEN` | _必填_ | 调查员 Bearer token |
| `ANALYST_TOKEN` | _必填_ | 分析师 Bearer token（必须与调查员不同） |
| `SAMPLES_DIR` | `/data/samples` | 样例目录 |
| `GORM_VERBOSE` | `0` | `1` 时打印 SQL |

---

## 6. 数据模型

```
cases(id, name, description, genesis_hash, created_at, updated_at)
evidence(id, case_id, source_path, real_path, filename, size, sha256,
         file_mode, device_id, inode, custodian, registered_by,
         registered_seq, created_at, updated_at,
         UNIQUE(case_id, real_path))
review_jobs(id, evidence_id, case_id, status, offset, prefix_sha256,
            final_sha256, baseline_sha256, expected_size, expected_real_path,
            expected_device_id, expected_inode, result, error_message,
            started_by, chain_seq, last_chunk_time, completed_at, …)
chain_events(id, case_id, seq, event_type, actor, prev_digest,
             content_json, digest, created_at, UNIQUE(case_id, seq))
```

---

## 7. 测试覆盖

`go test ./...`（纯 Go，sqlite 内存/临时库，无需外部服务）覆盖需求中的六类场景：

| 场景 | 测试 |
|---|---|
| 读取中镜像变化 → 登记失败、无错误基线 | `TestRegisterFailsWhenFileChangesDuringRead`、`TestHashAndVerifyChangedDuringRead`、`TestShrinkDuringRead` |
| 恢复前镜像被修改 / 替换 / 变大 | `TestReviewResumeRejectsModifiedPrefix`、`…ReplacedFile`、`…RejectsSizeChange` |
| 并发链追加不分叉、不丢事件 | `TestConcurrentAppendNoFork`（60 goroutine）、`TestConcurrentMultipleCasesIsolation` |
| 篡改 / 缺失 / 乱序检测 | `TestVerifyDetectsTamperingGapAndReorder`、`TestReviewDetectsTamperedBaseline` |
| 路径与符号链接越界 | `TestResolveWhitelistAndSymlinks`、`TestPathPrefixBoundary`、`TestRegisterRejectsPathEscape`、`TestReviewRejectsSymlinkSwap` |
| 角色权限限制 | `TestRolePermissions`（401/403/调查员/分析师矩阵） |
| 中断后续算成功 | `TestReviewInterruptedAndResumed` |
| 报告含基线/复核/全链/免责声明 | `TestReportAndChainEndToEnd`、`TestRolePermissions` |

带竞态检测：`make race`。

---

## 8. 目录结构

```
cmd/server      HTTP 服务入口（启动时恢复中断作业、优雅停止时写检查点）
cmd/genseed     样例镜像生成器
internal/
  config        环境配置、白名单根解析
  database      GORM 连接与 AutoMigrate（mysql / 纯 Go sqlite）
  auth          Bearer token 与角色中间件
  securefile    白名单/符号链接防护、只读打开、分块哈希+变化检测
  hashing       规范化 JSON 与 SHA-256 链接
  chain         追加、并发防分叉、Verify 完整性校验
  cases         案件登记（案件与创世事件同事务）
  evidence      基线登记（双遍哈希）、移交、备注
  review        持久化可恢复复核作业管理器
  report        案件导出（基线+复核+全链+校验+免责声明）
  api           Gin 路由与处理器
  testkit       测试夹具
samples         两个小 raw/dd 样例
docker          容器入口脚本
```

---

## 9. 运维说明与限制

- **时间**：所有时间戳来自服务器本地时钟（存 UTC）。链证明的是“按写入顺序的完整性”，
  不是“某事件在某个真实世界时刻发生”。需要可信时间请引入 RFC 3161 时间戳并把 TSA 回执
  作为载荷/外部签名保存——本项目不包含该功能。
- **签名**：链摘要是本机计算的完整性值，不涉及非对称密钥，不构成数字签名。
- **MySQL** 是生产目标；SQLite 仅为本地开发/测试，写连接被串行化以匹配其单写者模型。
- **删除策略**：案件/证据/事件不提供删除接口，链事件表在设计上只追加。
- 作业状态：`running` →（取消/重启）→ `interrupted` → `resume` → `completed`；
  读错误、身份/前缀/尺寸不符 → `failed`（`error_message` 记录原因）。
