# chunk-upload — 不可变对象分块续传服务（Rust + Axum）

纯后端 HTTP 服务：客户端为一个不可变对象创建上传会话，按固定大小分块
（顺序任意、可重传）上传，服务端在“完成”时拼接所有块、校验**总长度**与
**整对象 SHA-256**，通过后原子发布；发布之前对象不可读。

## 语义与保证

| 需求 | 实现 |
| --- | --- |
| 上传 ID 隔离未提交块 | 每个会话有独立目录 `uploads/<upload_id>/`，块彼此隔离 |
| 乱序上传 | 块按编号（`0..n`）存放，与到达顺序无关；`GET /uploads/:id` 报告缺失块 |
| 同编号同内容重传幂等 | 以块 SHA-256 判定，内容相同返回 `200` + 原有记录 |
| 同编号异内容拒绝 | 返回 `409 part_conflict`，已存块不被覆盖 |
| 完成时校验总长度 + 整体哈希 | 拼接时二次校验每块哈希、累计长度，最后校验整对象 SHA-256；失败返回 `422`，不发布 |
| 发布前不可读 | `GET /objects/{key}` 仅在对象目录存在且元数据完整时返回数据，否则 `404` |
| 重复完成幂等 | 已提交的会话再次 complete 返回同一对象元数据（`201`） |
| 对象不可变 | 同 key 已发布后，再建会话返回 `409`；已提交会话不能再写块 |
| 崩溃恢复 | 块写入为 `tmp + fsync + rename`；启动时清扫残留 `.tmp`；对象先落盘再标记 committed；若 committed 标记丢失，重启时依据对象文件自动对账修复 |
| 零长度对象 | 支持（恰有一个 0 字节块） |

### 磁盘布局

```text
<data-dir>/
  uploads/<upload_id>/meta.json     # 会话元数据（原子替换）
  uploads/<upload_id>/parts/<n>     # 第 n 块（0 起编号，原子 rename 落盘）
  objects/<encoded-key>/object      # 发布后的不可变对象字节
  objects/<encoded-key>/meta.json   # {key,size,sha256,upload_id,created_at}
  tmp/<upload_id>.stage             # 完成时拼接的暂存文件，发布后删除
```

key 允许包含 `/`（如 `logs/2026/a.log`），每段做百分号编码，`..` 等无法逃逸
`objects/` 目录。

## 依赖与构建

- Rust（stable，开发使用 1.98.1）+ Cargo，无其他系统依赖
- 仅依赖：`axum`、`tokio`、`serde`/`serde_json`、`sha2`、`hex`、`uuid`、
  `tracing`/`tracing-subscriber`、`futures-util`（测试另用 `tempfile`、
  `tower`、`http-body-util`、`bytes`）

```bash
cargo build --release
```

锁定依赖见 `Cargo.lock`（已提交）。

## 启动

```bash
# 开发运行
cargo run -- --addr 127.0.0.1:8080 --data-dir ./data

# 或 release 二进制
./target/release/chunk-upload --addr 127.0.0.1:8080 --data-dir ./data
```

参数（均可省略，支持 `--k=v` 形式）：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--addr` | `0.0.0.0:8080` | 监听地址 |
| `--data-dir` | `./data` | 持久化目录（自动创建） |
| `--max-body` | `67108864`（64 MiB） | 单块请求体大小上限 |

日志级别可用 `RUST_LOG=debug` 调整。

## HTTP 接口

### 1. 创建上传会话

`POST /uploads`

```json
{
  "key": "videos/demo.bin",
  "total_size": 30,
  "sha256": "<整对象 64 位十六进制 sha256，大小写均可>",
  "chunk_size": 10
}
```

`chunk_size` 可选，默认 4 MiB。响应 `201`：

```json
{
  "upload_id": "9f2c…",
  "key": "videos/demo.bin",
  "total_size": 30,
  "sha256": "…",
  "chunk_size": 10,
  "part_size": 10,
  "parts": [ {"number":0,"size":0,"sha256":""}, … ],
  "chunks": [ {"start":0,"end":10}, {"start":10,"end":20}, {"start":20,"end":30} ],
  "created_at": 1789000000,
  "committed": false,
  "received_parts": [],
  "missing_parts": [0, 1, 2]
}
```

### 2. 查询会话状态

`GET /uploads/<upload_id>` → 返回同上结构，可用于断点续传时决定要补传哪些块。

### 3. 上传一个块（可乱序、可重传）

`PUT /uploads/<upload_id>/parts/<number>`

- 请求体为**裸字节**（`Content-Type: application/octet-stream`）
- 块长必须等于创建会话时该编号对应的 `end-start`（最后一块允许更短）
- 同内容重传 → `200`（幂等）；异内容 → `409 part_conflict`
- 编号越界/长度不符 → `400`；会话不存在 → `404`；已提交 → `409`

### 4. 完成（拼接 + 校验 + 发布）

`POST /uploads/<upload_id>/complete`

- 缺块 → `422 unprocessable_entity`（消息中列出缺失编号）
- 长度或整对象哈希不符 → `422`，对象不会发布、保持不可读
- 成功 → `201`：`{"key":…,"size":…,"sha256":…,"upload_id":…,"created_at":…}`
- 对已提交会话重复调用 → 幂等返回同一元数据

### 5. 读取已发布对象

`GET /objects/<key>`（key 中的 `/` 直接写在路径里）

- 发布前 / 不存在 → `404`
- 成功 → `200`，裸字节，响应头带 `X-Object-Sha256`、`ETag: "<sha256>"`
- 读取时再次比对磁盘长度与哈希，异常返回 `500` 而非错误数据

### 6. 中止上传

`DELETE /uploads/<upload_id>`（已提交的会话不可中止，返回 `409`）

## curl 端到端示例

```bash
# 准备一个 30 字节文件并计算整对象哈希
head -c 30 /dev/urandom > demo.bin
SIZE=$(stat -c%s demo.bin)
HASH=$(sha256sum demo.bin | cut -d' ' -f1)

# 1) 创建会话（每块 10 字节）
RESP=$(curl -sS -X POST localhost:8080/uploads \
  -H 'content-type: application/json' \
  -d "{\"key\":\"demo.bin\",\"total_size\":$SIZE,\"sha256\":\"$HASH\",\"chunk_size\":10}")
ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["upload_id"])')

# 2) 乱序上传：先第 2 块，再第 0 块，最后第 1 块
dd if=demo.bin of=p2 bs=1 skip=20 count=10 status=none
dd if=demo.bin of=p0 bs=1 skip=0  count=10 status=none
dd if=demo.bin of=p1 bs=1 skip=10 count=10 status=none
curl -sS -X PUT   localhost:8080/uploads/$ID/parts/2 --data-binary @p2
curl -sS -X PUT   localhost:8080/uploads/$ID/parts/0 --data-binary @p0
curl -sS -X PUT   localhost:8080/uploads/$ID/parts/1 --data-binary @p1

# 幂等重传（同内容，返回 200、同一 sha256）
curl -sS -X PUT   localhost:8080/uploads/$ID/parts/0 --data-binary @p0

# 3) 缺块时完成会 422；补齐后完成 → 201
curl -sS -X POST  localhost:8080/uploads/$ID/complete

# 4) 发布前读取是 404；发布后读取并校验
curl -sS -D - localhost:8080/objects/demo.bin -o got.bin
cmp demo.bin got.bin && echo "OK: bytes identical"
```

仓库内附带可直接运行的脚本：`examples/demo.sh`（正常流程，含乱序/幂等/重复
完成/重启演示）、`examples/failure_demo.sh`（缺块、异内容、错哈希、不可变）
与 `examples/crash_recovery.sh`（真实 SIGKILL ×2 的崩溃恢复验收）。

## 自动化测试

```bash
cargo test
```

`tests/api.rs` 通过 Axum router 在进程内驱动完整 HTTP 链路，覆盖：

1. 乱序上传后读取，字节与哈希一致（`out_of_order_upload_then_read`）
2. 缺块完成返回 422，状态接口列出缺失块，重启后仍不可读（`missing_parts_blocks_completion`）
3. 同块相同内容重传 200 幂等、不同内容 409（`part_reupload_identical_is_idempotent_different_rejected`）
4. 重复完成幂等，且重启后仍幂等（`repeated_complete_is_idempotent`）
5. 整对象哈希错误 → 422，重启后对象仍 404（`wrong_total_hash_is_rejected_and_unreadable_after_restart`）
6. 已发布对象不可变、已提交会话拒绝再写（`published_object_is_immutable`）
7. 崩溃恢复：残留 tmp 清扫、断点续传、committed 标记丢失后重启自动对账修复，
   且只能读到完整且哈希正确的对象（`restart_after_crash_heals_state`）
8. 零长度对象、未知会话 404、块长/编号非法 400

真实进程的崩溃恢复（非模拟）可用脚本复现，它对运行中的服务发两次
`kill -9`（上传中途、发布之后），验证断点续传与发布原子性：

```bash
cargo build
./examples/crash_recovery.sh ./target/debug/chunk-upload
# 期望末行: CRASH-RECOVERY ACCEPTANCE PASSED
```

实际运行结果（含三个示例脚本的真实输出）见 [TEST_RESULTS.md](TEST_RESULTS.md)。

## 设计说明与限制

- 单进程内用一把异步互斥锁串行化变更操作（会话/块/提交），结构清晰；
  持久化与崩溃安全由文件系统原子 rename + fsync 保证，锁不是崩溃安全的依赖。
- 当前每次读对象一次性载入内存（面向验收规模）；超大对象可改为 `ReaderStream`
  边读边校验/输出，接口无需变化。
- 未实现：分块独立上传时的 `Content-MD5` 请求头校验（以服务端实算 SHA-256
  代替）、上传会话过期 GC、鉴权/TLS（建议置于反向代理之后）。
