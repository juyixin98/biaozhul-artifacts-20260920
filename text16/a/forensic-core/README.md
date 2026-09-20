# ForensicCore — 本地证据处理后端

ForensicCore 是一个本地证据（磁盘镜像）处理后端：登记白名单目录内的 raw/dd 镜像并记录
SHA-256 基线，以持久化作业执行完整性复核（可中断恢复），并维护只追加的哈希链证据链。

**明确的边界（不做什么）**：不解析磁盘文件系统、不做删除恢复、不提供司法合规认证。
只接受 `.raw` / `.dd` 文件。

技术栈：Go 1.22 · Gin · GORM · MySQL 8 · Docker / docker compose。

## 快速开始

```bash
docker compose up --build
```

服务监听 `:8080`，MySQL 数据保存在 `mysql-data` 卷，`./samples` 以**只读**方式挂载为
证据根目录（容器层保证原始镜像不被修改）。`samples/` 内含两个小镜像样例，
可用 `samples/make_samples.sh` 重新生成（内容确定）。

本地开发（测试用内存 SQLite，无需 MySQL）：

```bash
go test ./...
go run ./cmd/server   # 需要 FORENSIC_MYSQL_DSN 指向一个 MySQL 实例
```

### 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FORENSIC_ADDR` | `:8080` | 监听地址 |
| `FORENSIC_MYSQL_DSN` | 见 `cmd/server/main.go` | MySQL DSN |
| `FORENSIC_EVIDENCE_ROOT` | `./samples` | 证据白名单根目录 |
| `FORENSIC_CHUNK_SIZE` | `4194304` | 分块大小（字节） |

### 认证与角色（演示用途）

每个请求携带两个头：

```
X-User-Name: inv1
X-User-Role: investigator   # 或 analyst
```

- **调查员 investigator**：登记证据、发起/恢复复核、移交、备注、查询。
- **分析师 analyst**：只能查询和备注，其余变更操作返回 403。

## API 一览

| 方法 | 路径 | 角色 | 说明 |
|---|---|---|---|
| POST | `/api/cases` | investigator | 建案 `{name, description}` |
| GET | `/api/cases` | 任意 | 案件列表 |
| POST | `/api/cases/:id/evidence` | investigator | 登记镜像 `{path}`（相对证据根目录） |
| GET | `/api/cases/:id/evidence` | 任意 | 基线列表 |
| POST | `/api/evidence/:id/verify-jobs` | investigator | 创建并执行复核作业 |
| POST | `/api/verify-jobs/:id/resume` | investigator | 恢复中断的作业 |
| GET | `/api/verify-jobs/:id` | 任意 | 作业进度/结果/错误 |
| POST | `/api/cases/:id/transfers` | investigator | 移交 `{to, note}` |
| POST | `/api/cases/:id/notes` | 任意 | 备注 `{text}` |
| GET | `/api/cases/:id/chain` | 任意 | 完整证据链 |
| GET | `/api/cases/:id/chain/verify` | 任意 | 链校验（缺失/篡改/乱序） |
| GET | `/api/cases/:id/report` | 任意 | 导出报告（基线+复核+证据链） |

### 示例

```bash
H_INV='-H "X-User-Name: inv1" -H "X-User-Role: investigator"'
curl -s -X POST localhost:8080/api/cases \
  -H "X-User-Name: inv1" -H "X-User-Role: investigator" \
  -d '{"name":"case-001","description":"示例案件"}'
curl -s -X POST localhost:8080/api/cases/1/evidence \
  -H "X-User-Name: inv1" -H "X-User-Role: investigator" \
  -d '{"path":"sample-small.dd"}'
curl -s -X POST localhost:8080/api/evidence/1/verify-jobs \
  -H "X-User-Name: inv1" -H "X-User-Role: investigator"
curl -s localhost:8080/api/cases/1/report \
  -H "X-User-Name: ana1" -H "X-User-Role: analyst"
```

## 关键设计

### 1. 登记：只读分块哈希 + 变更检测

- 路径安全：拒绝绝对路径与 `..` 穿越；`EvalSymlinks` 解析整条路径后必须仍落在
  白名单根目录内（符号链接逃逸被拒）；打开后通过 `lstat`/`fstat` 一致性与
  `/proc/self/fd` 复查抵御 stat→open 竞争；只接受 `.raw`/`.dd` 普通文件，O_RDONLY 打开。
- 分块（默认 4 MiB）计算 SHA-256；**读完后重新 fstat**，大小/mtime/ctime 任一变化
  即判定"读取期间被修改"，登记失败且**不保存任何基线**（证据与链事件在同一事务内，
  失败整体回滚）。
- 基线同时记录 dev/inode/mtime/ctime，作为后续身份校验依据。

### 2. 复核作业：持久化、可恢复

- 作业状态（`pending/running/paused/completed/failed`）、进度（已处理字节）、
  每个分块的 SHA-256 都持久化在 MySQL；进程重启后 `running` 作业自动转为 `paused`。
- **恢复（resume）前的校验**：
  1. 文件身份：dev + inode + 大小与基线一致（不靠文件名）；
  2. 已有进度时额外要求 mtime/ctime 与基线一致（识别"同名同内容但已替换"，
     inode 号可能被复用）；
  3. **重算已处理前缀**，与持久化的分块摘要逐一比对，确认已处理部分未变化后才继续。
  任一不符 → 作业失败并记录错误，需新建作业从头复核。
- 复核全程结束再次 fstat，过程中发生变化则结果作废（作业失败）。
- 最终摘要与基线比较，结论为 `match` / `mismatch`，并写入证据链。

### 3. 证据链：只追加哈希链

- 事件类型：`register` / `verify` / `transfer` / `note`。
- 每条事件含：案件内序号 `seq`、前一事件摘要 `prev_hash`、规范化内容摘要 `hash`
  （canonical JSON：键排序，覆盖 seq/type/actor/payload/prev_hash/时间戳）。
- **并发不分叉**：追加在事务内先 `SELECT ... FOR UPDATE` 锁定案件行序列化追加；
  `(case_id, seq)` 唯一索引兜底，冲突时重试。
- `GET /api/cases/:id/chain/verify` 重放全链：序号连续性（**缺失**）、
  逐条重算内容摘要（**篡改**）、prev_hash 链接（**乱序**）。

### 4. 限制与声明

- **哈希链只能提供完整性检查**（事后发现缺失/篡改/乱序），事件时间来自服务器本地时钟，
  **不能代替可信时间戳（TSA）或外部数字签名**；要证明"某事件在某时刻已存在"，
  需要把链头摘要定期锚定到外部可信时间源。
- 读取期间的变更检测基于 fstat 前后比对（大小/mtime/ctime），
  受文件系统时间戳粒度限制；对抗具备 root 权限、能同时伪造内容与时间戳的攻击者
  需要写保护介质与外部签名，超出本系统范围。
- 认证为演示用途的请求头，生产环境应替换为真实的身份认证与审计。
- 复核作业在当前请求内同步执行（小镜像场景）；大镜像可在此基础上改为后台 worker，
  进度与恢复语义不变。

## 测试

```bash
go test -race ./...
```

覆盖（`internal/*/*_test.go`）：

- **读取中变化**：登记/复核过程中文件被修改 → 失败且不留基线（hashutil、evidence）。
- **恢复前修改**：中断后篡改已处理部分、替换整个文件、篡改分块进度记录 → 续算被拒绝。
- **并发链追加**：8 协程 × 10 事件并发追加 → 序号恰好 1..N、链校验通过、无分叉。
- **篡改检测**：改事件内容 → `tampered`；删中间事件 → `missing`；交换序号 → `out_of_order`。
- **路径越界**：`..` 穿越、绝对路径、符号链接逃逸（文件/目录）、非 raw/dd 扩展名。
- **权限限制**：分析师登记/复核/移交 → 403，查询/备注 → 200；无认证头 → 401。

## 目录结构

```
cmd/server/          入口（配置、迁移、中断作业恢复）
internal/api/        Gin 路由、认证与角色中间件、报告导出
internal/chain/      只追加哈希链（追加/校验）
internal/evidence/   登记与复核作业
internal/hashutil/   分块哈希与文件身份/变更检测
internal/safeopen/   白名单根目录内的安全路径解析与只读打开
internal/models/     GORM 模型
internal/db/         MySQL 连接与迁移
samples/             小镜像样例（64KiB dd + 32KiB raw）与生成脚本
```
