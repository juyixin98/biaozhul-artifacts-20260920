# ForensicCore — 本地证据处理后端

使用 **Go + Gin + GORM + MySQL** 实现的本地取证镜像处理后端，提供 Docker 一键启动。

**明确的范围边界：**

- 只接收原始磁盘镜像 **raw/dd** 文件；
- **不**实现磁盘/文件系统解析、文件恢复、删除数据恢复；
- **不**做任何司法合规认证。

## 安全模型与关键设计

### 1. 案件登记：只读、分块、变化即失败

- 镜像必须位于配置的**白名单目录**内，登记时只提供「逻辑根名 + 相对路径」。
- 安全打开（`internal/safeopen`，Linux `openat`）：
  - 逐级 `openat(O_PATH|O_NOFOLLOW|O_DIRECTORY)` 解析中间目录，**任何一级是符号链接都拒绝**；
  - 词法校验相对路径，拒绝绝对路径、空分量、`.`、`..`，杜绝路径穿越越界；
  - 末级 `O_NOFOLLOW` 打开并 `fstat` 确认是常规文件。
- 分块 SHA-256（默认 4 MiB/块），全程 **`O_RDONLY`**，原始镜像绝不被修改。
- 登记采用**两遍读取 + 读前/读中/读后 `fstat`**：
  - 记录文件身份 `dev/ino/size/mtime/nlink` 与大小、SHA-256；
  - 计算期间文件被追加、截断或原地改写（身份变化或两遍摘要不一致）→ **登记整体失败，不保存错误基线，也不写链事件**。

### 2. 完整性复核：持久化作业，可中断恢复

复核是后台持久化作业（`verify_jobs` + `verify_chunks`），逐块保存进度与每块独立摘要。

- 中断（进程重启/停止）后服务重启会自动重新排队，也可显式调用 `resume`。
- **续算前必须验证两件事**，而不是只看文件名或大小：
  1. **文件身份**：重新打开后 `dev+ino` 必须与登记时一致（路径被同大小的另一个文件替换会被识别），且 `size` 一致；
  2. **已处理部分**：逐块重读已处理区间，重算块 SHA-256 并与持久化记录逐一比对；任何一块不符即判失败，不复用旧进度。
- 全部读完后再次 `fstat`，并用整文件摘要与登记基线比对；作业保存 `processed_size / final_sha256 / last_error` 等进度与错误。

### 3. 证据哈希链：只追加、并发不分叉

- 链按案件组织，只允许四类事件：`register` / `verify` / `transfer` / `note`。
- 每条事件包含：案件内连续**序号**、**前一事件摘要**、规范化内容摘要：
  - 规范化信封 `{type, sequence, prev, actor, at, payload}`，JSON 键排序、紧凑编码后求 SHA-256；
  - 首事件前驱为 64 个 `0`。
- 并发追加在**案件行锁（MySQL `FOR UPDATE`；测试用 SQLite 单连接串行）**内完成「读尾 → 序号 +1 → 写入」，因此不会分叉。
- 校验接口检测：**缺失**（序号不连续）、**篡改**（规范化内容或摘要被改）、**乱序/分叉**（prev 链接断裂、时间戳严格倒退）。

> ⚠️ **能力边界（重要）**：哈希链**只能提供完整性检查**。事件时间来自本服务本地时钟，
> **不构成可信时间戳**，链上摘要也**不能代替外部数字签名/时间戳服务（TSA）**。
> 该声明同时写入每份导出报告。

### 4. 角色权限（RBAC）

| 能力 | 调查员 investigator | 分析师 analyst |
|------|:---:|:---:|
| 建案件 / 登记镜像 / 发起复核 / 移交 | ✅ | ❌（403） |
| 查询案件/证据/作业/链、链校验、导出报告 | ✅ | ✅ |
| 备注 note | ✅ | ✅ |

认证使用 JWT（HS256）。

## 目录结构

```
cmd/server      HTTP 服务入口
cmd/mksample    生成确定性小镜像样例
internal/config 环境变量配置
internal/store  GORM 连接与迁移（MySQL；sqlite 仅供测试）
internal/model  数据模型
internal/safeopen openat 安全打开（防路径/符号链接越界）
internal/hashfile 只读分块两遍基线哈希
internal/chain  append-only 哈希链与校验
internal/service 业务编排、持久化复核作业与恢复、报告导出
internal/api    Gin 路由、JWT、RBAC、处理器
```

## 快速开始（Docker Compose）

```bash
# 1. 生成小镜像样例（已附带 samples/ 可跳过）
go run ./cmd/mksample -dir ./samples

# 2. 启动 MySQL + API（samples 目录以只读方式挂进容器 /evidence）
docker compose up --build
```

服务监听 `http://localhost:8080`。生产请通过环境变量覆盖
`FORENSIC_JWT_SECRET` 与 `FORENSIC_USERS` 中的默认密码。

### 本地直接运行（用 SQLite，免 MySQL，仅用于开发）

```bash
go run ./cmd/mksample -dir ./samples
FORENSIC_DB_DRIVER=sqlite FORENSIC_DB_DSN=forensic.db \
FORENSIC_ROOTS="evidence=$(pwd)/samples" \
go run ./cmd/server
```

## API 速览

先获取令牌：

```bash
curl -s -X POST localhost:8080/api/v1/auth/token \
  -H 'Content-Type: application/json' \
  -d '{"username":"investigator","password":"invest123"}'
```

| 方法 | 路径 | 角色 | 说明 |
|------|------|------|------|
| POST | `/api/v1/auth/token` | 公开 | 登录获取 JWT |
| POST | `/api/v1/cases` | 调查员 | 建案件 |
| GET | `/api/v1/cases`、`/api/v1/cases/:caseId` | 两者 | 查询 |
| POST | `/api/v1/cases/:caseId/evidences` | 调查员 | 登记镜像（白名单内 raw/dd） |
| GET | `…/evidences`、`…/evidences/:evidenceId` | 两者 | 查询基线 |
| POST | `…/evidences/:evidenceId/verify` | 调查员 | 创建复核作业（202，异步执行） |
| POST | `/api/v1/cases/:caseId/jobs/:jobId/resume` | 调查员 | 恢复中断作业 |
| GET | `…/jobs`、`…/jobs/:jobId` | 两者 | 查询作业进度/错误/分块 |
| POST | `…/evidences/:evidenceId/transfer` | 调查员 | 移交保管 |
| POST | `/api/v1/cases/:caseId/notes` | 两者 | 追加备注 |
| GET | `…/chain` | 两者 | 完整证据链 |
| GET | `…/chain/verify` | 两者 | 缺失/篡改/乱序校验 |
| GET | `…/report?format=json\|markdown` | 两者 | 导出：基线+复核+完整链+能力声明 |

### 端到端示例

```bash
TOKEN=$(curl -s -X POST localhost:8080/api/v1/auth/token -H 'Content-Type: application/json' \
  -d '{"username":"investigator","password":"invest123"}' | jq -r .token)
AUTH="Authorization: Bearer $TOKEN"

curl -s -X POST localhost:8080/api/v1/cases -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"case_number":"C-001","title":"演示案件","custodian":"alice"}'

curl -s -X POST localhost:8080/api/v1/cases/1/evidences -H "$AUTH" -H 'Content-Type: application/json' \
  -d '{"root_name":"evidence","rel_path":"disk-a.raw"}'

curl -s -X POST localhost:8080/api/v1/cases/1/evidences/1/verify -H "$AUTH"
curl -s localhost:8080/api/v1/cases/1/jobs -H "$AUTH"
curl -s "localhost:8080/api/v1/cases/1/report?format=markdown" -H "$AUTH"
```

登记请求体中 `rel_path` 必须是挂载根内的相对路径（如 `sub/disk-c.raw`）；
`..`、绝对路径、符号链接一律拒绝。

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `FORENSIC_HTTP_ADDR` | `:8080` | 监听地址 |
| `FORENSIC_DB_DRIVER` | `mysql` | `mysql` 或 `sqlite`（仅测试/开发） |
| `FORENSIC_DB_DSN` | 指向 compose 的 mysql | 数据库 DSN |
| `FORENSIC_ROOTS` | `/data/evidence` | 白名单根，`name=/path;name2=/path2` |
| `FORENSIC_CHUNK_SIZE` | `4194304` | 分块大小（≥4096） |
| `FORENSIC_JWT_SECRET` | 开发默认值 | JWT 签名密钥，生产必改 |
| `FORENSIC_TOKEN_TTL_HOURS` | `12` | 令牌有效期 |
| `FORENSIC_USERS` | 两个示例账号 | `user:pass:role;...` |

## 测试

```bash
go test ./...          # 全部测试
go test -race ./...    # 竞态检测
```

覆盖场景（见 `internal/service/servicetest` 与 `internal/api`）：

- **读取中变化**：登记两遍哈希进行中改写/追加文件 → 登记失败且无基线落库；
- **恢复前修改**：中断后改写「已处理部分」→ 恢复时块摘要不符，作业失败；
- **恢复前替换**：同路径同大小换成另一个 inode → 文件身份校验失败；
- **基线后篡改**：复核出 digest mismatch；
- **并发链追加**：30 个 goroutine 并发追加，序号连续、不分叉、校验健康；
- **篡改/缺失/乱序检测**：直接改库内容、删中间事件、改 prev 链接均可被检出；
- **路径越界**：`..`、绝对路径、空分量、根内符号链接、符号链接目录均拒绝；
- **权限限制**：分析师登记/复核/移交返回 403，查询/备注允许；无令牌 401。
