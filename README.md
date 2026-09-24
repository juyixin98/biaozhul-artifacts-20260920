# 层引用垃圾回收（Layer-Reference Garbage Collection）

一个**本地内容寻址（content-addressable）镜像仓库**的安全垃圾回收实现：纯后端，
Go + 标准库 `net/http` + PostgreSQL。manifest 引用共享 blob；标签更新与拉取会产生
引用和**读租约（read lease）**；GC 在一致快照上**标记（mark）**，删除前再**核验
（re-verify）新引用与活跃租约**，保证**绝不会删除扫描期间刚被引用的层**。

所有 digest 校验都是对传输字节执行的**真实 SHA-256**，没有任何桩实现。

---

## 1. 安全模型（本项目的核心）

数据模型对应 OCI Distribution v2：

- `blobs` —— 全局共享、内容寻址的层 / config；
- `manifests` —— 每个仓库一份、自身也按字节摘要寻址的清单（存为 `BYTEA`）；
- `manifest_refs` —— manifest → 子对象（blob 或子 manifest）的有向边；
- `tags` —— 可变的命名引用；
- `uploads` —— 可续传分块上传会话（暂存区）；
- `leases` —— 拉取期间钉住对象的读租约。

GC 分两阶段，并借助 PostgreSQL 的两类机制保证安全：

### 阶段一：MARK（一致快照）

单个 `REPEATABLE READ`（默认隔离级即可，因为只用一次快照）事务：

1. 先取**全局发布 advisory lock**（`pg_advisory_xact_lock`）；
2. 记录 `pg_current_xact_id()` 作为快照栅栏（fence）；
3. 用一个**递归 CTE** 从「所有 tag」+「所有未过期读租约」出发，沿 `manifest_refs`
   遍历整个 manifest DAG，得到该快照下的**可达集合**；
4. 快照内所有 blob/manifest 与可达集合比对，划分「保留」与「候选删除」。

### 阶段二：SWEEP（删除前逐条复核）

对每个候选对象，开一个**全新事务**并：

1. 再次取**全局发布锁** —— 与「发布」建立全序。任何在 mark 快照之后提交的
   发布（新 tag、新 manifest、blob 定稿）此刻一定可见；
2. 取**该对象自己的 advisory lock** —— 与「进行中的拉取」建立全序；
3. 用**当前最新已提交状态**重算可达性，并检查活跃读租约；
4. 只有仍然不可达、且无活跃租约，才删除。

因此：**只要某层在删除提交之前的任何时刻被引用，它就会被保留**
（审计原因为 `retained-after-mark: …`）。

### 崩溃顺序（all-or-nothing）

blob 的字节在磁盘上。删除时严格按以下顺序：

1. 先把文件 `rename()` 进「每次运行独立的隔离目录」`quarantine/<run>/`；
2. 再在事务里删除数据库行并提交；
3. 提交成功后才物理 `unlink` 隔离文件。

崩溃恢复（启动时 `-recover` 或 `POST /admin/recover`）对每个隔离对象二选一：

- 数据库行还在（事务回滚）→ 把文件**还原**回 CAS 树；
- 数据库行已删除（事务已提交）→ **清除**隔离文件。

无论死在哪一步，结果都是「行与文件一致」，不会出现「有行无文件」或误删。

### 上传：先临时落盘、验 digest、再发布

- 上传字节先进 `tmp/<upload-id>`（支持 `POST` 初始化 / `PATCH` 分块续传 /
  `PUT ?digest=` 定稿，也支持单次 `POST …/uploads/?digest=` 直传）；
- 定稿时对暂存文件做一次**真实 SHA-256 全量校验**，不符则拒绝（`DIGEST_MISMATCH`），
  且永不进入 CAS 树；
- 校验通过后同文件系统**原子 rename** 到 `blobs/sha256/<ab>/<digest>`，再写库行。
  崩溃只可能留下孤儿临时文件或内容正确的 stray 文件，绝不产生损坏的已发布 blob。

### 孤儿临时文件单独处理

暂存文件不是内容寻址的，按**上传会话年龄**独立回收（`POST /admin/orphans`）：
过期会话的临时文件 + 没有任何会话行指向的游离临时文件都会被删除并审计，
活跃会话的暂存文件始终保留。

---

## 2. 目录结构

```
cmd/registry/            程序入口（HTTP 服务 + 启动恢复）
internal/digestx/        sha256:<hex> 摘要、流式校验器（真实加密哈希）
internal/manifestx/      OCI/Docker manifest 解析与引用图提取
internal/storage/        磁盘 CAS 布局、暂存、隔离/还原/清除、fsync
internal/store/          PostgreSQL：schema(embed)、advisory lock、可达性 CTE、CRUD
internal/gc/             两阶段 GC、崩溃恢复、孤儿清理
internal/registry/       HTTP handler（上传/拉取/清单/tag/租约/GC 管理接口）
scripts/                 setup-db.sh、acceptance.sh、smoke.sh、race.sh
examples/                清单模板与运行时生成的示例输入
```

---

## 3. 本地启动

前置：Go 1.22+、PostgreSQL 14+（用到递归 CTE 的 `CYCLE` 子句；本仓库在 PG 16 验证）。

```bash
# 1) 建角色和库（幂等；按你的环境调整密码）
sudo -u postgres bash scripts/setup-db.sh
#   -> 角色 registry / 库 registry 与 registry_test

# 2) 编译
go build -o bin/registry ./cmd/registry

# 3) 启动（schema 自动迁移；-faults 仅为验收开启故障注入头）
DATABASE_URL='postgres://registry:registry_pw@127.0.0.1:5432/registry?sslmode=disable' \
  ./bin/registry -addr :18081 -data ./data -faults -blob-grace 0s
```

启动参数：`-addr`、`-db`、`-data`、`-lease-ttl`（默认 60s）、
`-blob-grace`（新 blob 宽限期，默认 30s）、`-orphan-max-age`（默认 2h）、
`-faults`（开启测试用故障注入）、`-recover`（启动时先做崩溃恢复）。

健康检查：`curl http://127.0.0.1:18081/healthz` → `ok`。

---

## 4. 验收命令

### 一键验收（覆盖题目要求的全部场景，28 个断言）

先按上面启动监听 `:18081`、带 `-faults` 的服务，然后：

```bash
bash scripts/acceptance.sh
# 结尾应输出：PASS=28 FAIL=0，退出码 0
```

它会真实地：两镜像共享一层并核对保留原因；制造「慢拉取 vs GC」竞争；
在 mark/sweep 间隙发布新标签；用**真实进程退出（SIGKILL 语义）**制造
提交前/提交后两种崩溃并重启恢复；清理孤儿临时文件；验证错误 digest 被拒；
最后打印审计事件统计。

### Go 自动化测试

```bash
# 纯单元测试（不需要数据库）
go test ./...

# 含 PostgreSQL 的集成/并发测试（需要 registry_test 库）
RUN_PG_TESTS=1 go test -race -count=1 ./...
```

集成测试覆盖：共享层保留、拉取/删除竞争、标记后新标签、分块上传与 offset 校验、
digest 拒绝、孤儿清理、两种崩溃恢复，以及一个 3 路（GC/发布/拉取）并发压力测试。

---

## 5. HTTP 接口速览

| 方法 & 路径 | 说明 |
| --- | --- |
| `POST /v2/{repo}/blobs/uploads/` | 初始化可续传上传 |
| `PATCH /v2/{repo}/blobs/uploads/{id}` | 追加分块（带 offset / Content-Range 校验） |
| `PUT /v2/{repo}/blobs/uploads/{id}?digest=sha256:…` | 真实验 digest 并原子发布 |
| `POST /v2/{repo}/blobs/uploads/?digest=sha256:…` | 单次直传（先暂存、验完再发布） |
| `HEAD/GET /v2/{repo}/blobs/{digest}` | 查询 / 拉取（拉取期间持有对象锁+读租约） |
| `DELETE /v2/{repo}/blobs/{digest}` | 删除无引用且无租约的 blob |
| `PUT /v2/{repo}/manifests/{tag 或 digest}` | 发布清单（校验引用完整性，产生 tag 引用与租约） |
| `GET/HEAD/DELETE /v2/{repo}/manifests/{ref}` | 拉取 / 删除清单 |
| `GET /v2/{repo}/tags/list` | 列标签 |
| `POST /v2/{repo}/leases`、`POST/DELETE /v2/{repo}/leases/{id}` | 显式读租约 |
| `POST /admin/gc` | 执行一次 mark+sweep（见下） |
| `GET /admin/gc`、`GET /admin/gc/{id}` | 运行历史 / 单次运行的逐项保留-删除原因 |
| `POST /admin/orphans` | 回收孤儿临时文件 |
| `POST /admin/recover` | 崩溃恢复 + stray 文件核对 |
| `GET /admin/blobs`、`GET /admin/audit` | blob 目录 / 追加式审计事件流 |

`POST /admin/gc` 请求体（字段均可选）：

```json
{ "grace_seconds": 0,
  "mark_sweep_delay": 0,
  "fault": "before_commit | after_commit | kill_before_commit | kill_after_commit" }
```

`fault` 仅在 `-faults` 下生效；`kill_*` 会让进程在对应崩溃窗口**真正退出**，
用于端到端验证重启恢复（`acceptance.sh` 使用）。

---

## 6. 可审计的保留 / 删除原因

每个被考虑的对象都会在 `gc_items` 落一条决策，并在 `gc_audit_events` 追加事件。
原因是人类可读的，例如：

- `retained: referenced by live manifest(s) alpha@sha256:ab12…, beta@sha256:cd34…`
  （共享层被两个镜像引用）
- `retained: tag(s) v1 in repo alpha`
- `retained: read lease 3f9a… protect it at sweep recheck`（拉取中）
- `retained-after-mark: …`（**标记时不可达，但删除前复核发现已被引用/有租约**）
- `deleted: unreachable at mark snapshot and unreachable/unleased at sweep recheck`
- 恢复：`gc.recover_restore` / `gc.recover_purge`，并在 `gc_runs.note` 留痕。

查看：`curl http://127.0.0.1:18081/admin/gc/<run-id>` 与
`curl 'http://127.0.0.1:18081/admin/audit?limit=200'`。

---

## 7. 手工快速体验

```bash
B=http://127.0.0.1:18081
echo hello > /tmp/l.bin
D=sha256:$(sha256sum /tmp/l.bin | cut -d' ' -f1)
curl -X POST "$B/v2/demo/blobs/uploads/?digest=$D" --data-binary @/tmp/l.bin   # 201
curl "$B/v2/demo/blobs/$D"                                                      # 拉回字节
curl -X POST "$B/admin/gc" -d '{}' | python3 -m json.tool                       # 无引用 -> 下一次 GC 删除
```

错误 digest 会被真实哈希拒绝：

```bash
curl -X POST "$B/v2/demo/blobs/uploads/?digest=sha256:$(printf x | sha256sum | cut -d' ' -f1)" \
     --data-binary @/tmp/l.bin
# 400 DIGEST_MISMATCH: expected …, computed …
```

---

## 8. 设计取舍与边界

- 只实现了题目要求的纯后端与 OCI/Docker v2 manifest / index 的引用语义
  （含 referrers 的 `subject` 边）；未实现鉴权、TLS、分块跨节点复制。
- manifest 内容存 PostgreSQL `BYTEA`，blob 字节存本地磁盘；二者通过
  「先隔离文件、后删行、再清除」的顺序在崩溃下保持一致。
- 拉取在专用连接上持有**会话级对象 advisory lock** 直到字节发完，因此 GC 删除
  不可能与正在进行的字节流交错；读租约行是第二重、可审计的保护。
- 全局 GC 用会话 advisory lock 串行化（进程被杀死时 Postgres 自动释放，不会死锁）。
- 未使用任何云/对象存储；密码学仅为内容寻址所需的 SHA-256（标准库 `crypto/sha256`）。
