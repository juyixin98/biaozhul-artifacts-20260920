# ForensicCore — 本地证据处理后端

使用 **Go + Gin + GORM + MySQL** 实现的本地取证证据处理服务，提供 Docker
一键启动。仅接收 **raw/dd** 原始镜像；**不实现**磁盘文件系统解析、删除
文件恢复，也不提供司法/合规认证。

## 能力范围

- **案件登记**：只允许登记白名单目录（`FORENSIC_EVIDENCE_ROOT`）内的
  `.raw/.dd` 镜像；以 `O_RDONLY` 只读分块流式计算大小与 SHA-256。
  - 防路径越界：拒绝绝对路径、`..` 穿越、冒号/NUL 字符。
  - 防符号链接逃逸：对路径做 `EvalSymlinks`，解析结果必须仍在白名单根内；
    打开后用同一个文件描述符复核 inode/设备号，防 TOCTOU 替换。
  - 读取期间文件发生变化（大小/mtime/inode 变化、读取截断）→ 登记失败，
    **绝不写入错误基线**（案件与链事件同一事务，失败即整体不持久化）。
- **完整性复核（持久化作业）**：
  - 每个分块的 SHA-256 与已处理偏移随作业持久化；进程中断/重启后自动或手动恢复。
  - 续算前必须同时验证：① 文件身份（白名单相对路径重新解析 + 设备号/inode/
    大小/mtime 全部一致，不能只靠文件名或大小）；② **已处理部分每个分块
    重新哈希并与持久化摘要逐块比对**。任一不符即作业失败并保留错误码
    （`IDENTITY_MISMATCH` / `CONTENT_MODIFIED` / `BASELINE_MISMATCH`）。
- **证据链（只追加哈希链）**：
  - 事件类型：`register` / `verification` / `transfer` / `note`。
  - 每条含案件内连续序号 `seq`、前一条目摘要 `prev_digest`（创世为
    `SHA256("")`）、规范化内容摘要 `content_digest`（确定性 JSON 规范化，
    与字段顺序无关）和条目摘要 `entry_digest`。
  - 每案件用「插入即占锁」的咨询锁串行化并发追加（MySQL/InnoDB 与 SQLite
    均可工作），并发追加不会分叉。
  - 校验接口检测缺失（MISSING）、乱序/重复（OUT_OF_ORDER）、内容或摘要
    篡改（TAMPERED，含前序链接断裂）。
- **角色**（Bearer Token，配置在 `FORENSIC_AUTH`）：
  - `investigator` 调查员：登记、移交、发起复核、备注、查询。
  - `analyst` 分析师：仅查询与备注。
- **原始镜像只读不修改**：应用层始终 `O_RDONLY`；compose 还以 `:ro` 只读
  挂载证据目录做双重保证。
- **导出报告**：包含登记基线、全部复核作业结果、完整证据链（含每条摘要）
  以及局限性声明。

> **哈希链的局限（务必阅读）**：SHA-256 哈希链只能提供**完整性与顺序**
> 检查。它**不能**提供可信时间（主机时钟可被任意设置）、不可否认性或来源
> 证明；这些需要外部可信时间戳（如 RFC 3161）和/或受认可密钥的数字签名，
> 以及相应的司法/取证认可程序。本系统不构成合法证据保管链认证。

## 快速开始（Docker Compose）

```bash
# 生成两个小镜像样例到 samples/（compose 会只读挂载到 /data/evidence）
go run ./cmd/gensamples samples

docker compose up --build
```

服务监听 `http://localhost:18088`（容器内 8080），MySQL 监听在宿主
`127.0.0.1:13308`（容器内 3306）。宿主端口在 `docker-compose.yml` 中可改，
避免与本机已占用端口冲突。
compose 内置两个令牌（**仅限本地演示，生产请自行修改**）：

| 角色 | 名称 | Token |
|---|---|---|
| investigator | alice | `investigator-token` |
| analyst | bob | `analyst-token` |

### 示例调用

```bash
# 登记
curl -s -X POST localhost:18088/api/v1/cases \
  -H "Authorization: Bearer investigator-token" -H 'Content-Type: application/json' \
  -d '{"case_ref":"CASE-001","file":"sample.raw"}'

# 查询 / 证据链 / 校验
curl -s -H "Authorization: Bearer analyst-token" localhost:18088/api/v1/cases
curl -s -H "Authorization: Bearer analyst-token" localhost:18088/api/v1/cases/1/events
curl -s -H "Authorization: Bearer analyst-token" localhost:18088/api/v1/cases/1/verify

# 移交、备注、发起复核
curl -s -X POST localhost:18088/api/v1/cases/1/transfers \
  -H "Authorization: Bearer investigator-token" -H 'Content-Type: application/json' \
  -d '{"from":"evidence locker","to":"lab-7","reason":"disk analysis"}'
curl -s -X POST localhost:18088/api/v1/cases/1/notes \
  -H "Authorization: Bearer analyst-token" -H 'Content-Type: application/json' \
  -d '{"body":"imaging verified visually"}'
curl -s -X POST localhost:18088/api/v1/cases/1/verifications \
  -H "Authorization: Bearer investigator-token" -H 'Content-Type: application/json' \
  -d '{"chunk_size":1048576}'

# 复核进度/结果
curl -s -H "Authorization: Bearer analyst-token" localhost:18088/api/v1/cases/1/verifications

# 导出完整报告
curl -s -H "Authorization: Bearer analyst-token" localhost:18088/api/v1/cases/1/export
```

## 本地开发

```bash
# 1) 起一个 MySQL（也可用 Docker 只跑数据库）
docker compose up -d mysql

# 2) 配置环境变量并运行
export FORENSIC_DB_DSN='forensic:forensicpw@tcp(127.0.0.1:13308)/forensiccore?charset=utf8mb4&parseTime=True&loc=UTC'
export FORENSIC_EVIDENCE_ROOT="$PWD/samples"
export FORENSIC_AUTH='alice:investigator:investigator-token,bob:analyst:analyst-token'
go run ./cmd/server
```

### 配置项

| 环境变量 | 默认值 | 说明 |
|---|---|---|
| `FORENSIC_LISTEN` | `:8080` | 监听地址 |
| `FORENSIC_DB_DRIVER` | `mysql` | 数据库驱动（生产仅 mysql；测试用 sqlite） |
| `FORENSIC_DB_DSN` | — | GORM MySQL DSN（必填） |
| `FORENSIC_EVIDENCE_ROOT` | `/data/evidence` | 证据白名单目录 |
| `FORENSIC_CHUNK_SIZE` | `4194304` | 分块大小（字节） |
| `FORENSIC_POLL_INTERVAL_MS` | `500` | 作业轮询间隔 |
| `FORENSIC_AUTH` | — | `name:role:token` 逗号分隔，至少一个调查员 |

## 测试

测试使用纯 Go 的 SQLite 驱动（无需 CGO、无需外部数据库）：

```bash
go test ./... -count=1
```

覆盖场景：

- 读取/登记过程中文件内容变化 → 失败且不保存基线；
- 复核中断（ctx 取消模拟进程中断）后恢复 → 身份+已处理分块重验后续算成功；
- 恢复前修改已处理部分（保持大小与 mtime 不变）→ `CONTENT_MODIFIED`；
- 恢复前文件身份变化（mtime 改变）→ `IDENTITY_MISMATCH`；
- 同名同大小替换镜像 → `IDENTITY_MISMATCH`/`BASELINE_MISMATCH`；
- 12×5 并发 HTTP 追加备注 → 无分叉、无缺失、链校验通过；
- 直接篡改持久化行（payload / 摘要 / 删除事件 / 重排序号）→ 校验接口报出
  MISSING / TAMPERED / OUT_OF_ORDER；
- `..` 穿越与符号链接逃逸（文件与目录两种）被拒；FIFO/目录被拒；
- 分析师调用登记/移交/复核返回 403，但可查询与备注；无令牌/错令牌 401。

## 目录结构

```
cmd/server       HTTP 服务入口
cmd/gensamples   生成确定性小镜像样例
internal/config  环境变量配置与角色令牌
internal/models  GORM 模型
internal/database 数据库连接与迁移
internal/safeio  白名单路径解析、只读分块哈希、变化检测
internal/chain   只追加哈希链（规范化摘要、锁、校验）
internal/jobs    持久化复核作业、进度、中断恢复
internal/service 案件登记/移交/备注/报告用例
internal/auth    Bearer 鉴权与角色中间件
internal/api     Gin 路由与处理器
internal/report  导出报告
samples          小镜像样例（由 cmd/gensamples 生成）
```
