# 不可变对象分块续传服务（Rust + Axum）

纯后端 HTTP 服务：客户端把一个**不可变大对象**切成若干块，按任意顺序、可断点续传地上传；
服务端在“完成（finish）”时把所有分块按序串接，重新计算**总长度与整体 SHA-256**，
与创建会话时声明的值一致才原子发布。发布后的对象以其内容哈希为 ID，只能整体读取，不可修改。

## 语义保证

| 需求 | 实现 |
|---|---|
| 上传 ID 隔离未提交块 | 每个会话独立目录 `uploads/<upload_id>/`，块名为 `<n>.part` |
| 完成时校验总长度与整体哈希 | finish 时按 0..k 顺序串接，重算字节数与 SHA-256，不符返回 409，不发布 |
| 同编号同内容重传幂等 | 返回 `200` + `"idempotent": true`，不重复写盘 |
| 同编号异内容拒绝 | 返回 `409 Conflict`，已有块保持不动 |
| 发布前不可读 | 对象只存在于 `objects/<sha256>`；未 finish 时 GET 一律 404 |
| 重复完成幂等 | 再次 finish 返回 200 + `"idempotent": true` |
| 发布前崩溃可恢复 | 所有写操作走“临时文件 → fsync → rename → fsync 目录”；重启时清理 `.tmp`、收养孤儿块 |
| 只允许读取完整且哈希正确的对象 | 对象文件由校验通过的串接结果原子 rename 产生，无其他写入路径 |

发布流程的崩溃窗口处理（见 `src/store.rs` 的 `recover()`）：

- 分块 `rename` 后、`meta.json` 更新前崩溃 → 重启时校验孤儿 `<n>.part` 的大小与哈希后**收养**；
- finish 写了一半临时对象 → 重启删除 `objects/.tmp.obj.*`，客户端需重新 finish（块都在）；
- `meta.json` 已标记 committed 但对象文件缺失（最极端窗口）→ 重启时按分块**重新组装发布**；
- 对象已发布后崩溃 → 对象天然可读，finish 仍幂等。

## 依赖

- Rust（开发与验证版本 **1.98.1**，edition 2021；理论上 ≥1.75 即可）
- 无数据库、无外部服务；持久化只用本地文件系统（建议本地盘 / 网络块存储，依赖 `rename` 原子性与目录 fsync）
- 主要 crate：`axum 0.8`、`tokio`（全功能）、`sha2`、`serde`/`serde_json`、`uuid`、`tracing`
- 测试额外使用：`reqwest`（阻塞客户端，rustls）、`tempfile`
- 锁定版本见 `Cargo.lock`

## 启动

```bash
cargo run --release

# 可选环境变量（默认值如下）
LISTEN_ADDR=0.0.0.0:8080   # 监听地址
DATA_DIR=./data            # 持久化目录（自动创建 uploads/ 与 objects/）
MAX_CHUNK_SIZE=67108864    # 单块上限 64 MiB（创建会话时也会预检）
RUST_LOG=info              # 日志级别
```

健康检查：`GET /healthz`。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/uploads` | 创建上传会话，请求体声明整体 sha256、总长度、每块大小 |
| `GET` | `/uploads/{upload_id}` | 查询会话状态（已收块、缺失块、是否完成） |
| `PUT` | `/uploads/{upload_id}/chunks/{index}?sha256=<块哈希>` | 上传/重传一个块，body 为块原始字节 |
| `POST` | `/uploads/{upload_id}/finish` | 完成：校验并原子发布 |
| `GET` | `/objects/{sha256}` | 读取已发布对象（未发布 404） |
| `HEAD` | `/objects/{sha256}` | 仅取元信息（Content-Length、X-Object-Sha256） |
| `GET` | `/healthz` | 健康检查 |

块编号从 **0** 开始且必须连续；`chunk_sizes` 之和必须等于 `total_size`，最后一块可更短。

错误响应统一为：

```json
{ "error": { "code": "bad_request|not_found|conflict|payload_too_large|internal_error", "message": "..." } }
```

状态码约定：400 参数/长度/缺块错误，404 会话或对象不存在，409 哈希/幂等冲突，413 超块大小。

## 请求样例（curl）

下面的样例假设把一个文件切成两块上传。完整可运行脚本见 [`examples/curl-demo.sh`](examples/curl-demo.sh)
（需要 `bash`、`curl`、`coreutils`（sha256sum/split/stat/mktemp）、`python3`）。

```bash
BASE=http://127.0.0.1:8080
FILE=video.bin
CHUNK=$((4 * 1024 * 1024))   # 4 MiB 一块

# 1) 切块并计算各块大小与哈希
split -b "$CHUNK" -d "$FILE" /tmp/chunk-
mapfile -t SIZES < <(stat -c%s /tmp/chunk-*)
WHOLE_SHA=$(sha256sum "$FILE" | cut -d' ' -f1)
SIZES_JSON=$(printf '%s\n' "${SIZES[@]}" | python3 -c 'import sys,json;print(json.dumps([int(x) for x in sys.stdin]))')

# 2) 创建会话
curl -sS -XPOST "$BASE/uploads" -H 'content-type: application/json' \
  -d "{\"sha256\":\"$WHOLE_SHA\",\"total_size\":$(stat -c%s "$FILE"),\"chunk_sizes\":$SIZES_JSON}"
# -> 201 {"upload_id":"...","chunk_count":N,...}

# 3) 按任意顺序传块（断点后重跑同一命令即幂等续传）
for i in 0 1; do
  f=$(printf '/tmp/chunk-%02d' "$i")
  sha=$(sha256sum "$f" | cut -d' ' -f1)
  curl -sS -XPUT "$BASE/uploads/$UID/chunks/$i?sha256=$sha" \
    -H 'content-type: application/octet-stream' --data-binary @"$f"
done

# 4) 查询状态 / 完成
curl -sS "$BASE/uploads/$UID"
curl -sS -XPOST "$BASE/uploads/$UID/finish"   # -> object_id = 整体 sha256

# 5) 下载校验（未 finish 前此处为 404）
curl -sS "$BASE/objects/$WHOLE_SHA" -o /tmp/out.bin
sha256sum /tmp/out.bin
```

各阶段响应示例：

```jsonc
// POST /uploads -> 201
{ "upload_id": "9f2c…-…", "chunk_count": 2, "max_chunk_size": 67108864,
  "put_chunk_url_template": "/uploads/9f2c…/chunks/{index}",
  "finish_url": "/uploads/9f2c…/finish" }

// PUT chunks/1 -> 201（重传同内容 -> 200）
{ "index": 1, "sha256": "…", "size": 12582912, "idempotent": false,
  "missing_chunks": [] }

// GET /uploads/{id}
{ "upload_id": "…", "sha256": "…", "total_size": 16777216, "chunk_count": 2,
  "chunks": { "0": {"index":0,"size":4194304,"sha256":"…"}, … },
  "missing_chunks": [], "committed": false }

// POST /finish -> 200
{ "upload_id": "…", "object_id": "<sha256>", "object_url": "/objects/<sha256>",
  "committed": true, "idempotent": false }
```

## 自动化测试

```bash
cargo test
```

测试为**真实 HTTP 端到端**：每个用例启动服务二进制（随机端口、临时 DATA_DIR），
其中崩溃用例对进程发 `SIGKILL` 后用同一数据目录重启，覆盖：

- 乱序上传后发布、下载（含 HEAD 与响应头）；
- 缺块时 finish 被拒（400），补齐后成功；发布前对象 404；
- 同编号同内容重传 200 幂等、异内容 409；块哈希错误 409 且不落记录；
- 整体哈希 / 总长度不符 409，且不产生对象；
- 重复 finish 幂等；
- 发布前崩溃 → 重启续传；块 rename 与 meta 更新之间崩溃 → 孤儿块被收养、`.tmp` 被清理；
- finish 中途崩溃残留临时对象 → 重启清理并允许重新 finish；committed 标记与对象文件不一致 → 重组发布；
- 已发布对象在重启后只读访问；
- ~9 MiB / 10 块的多帧流式上传下载；块大小超声明 400、超服务上限 413、非法参数 400/404。

单元测试（`src/store.rs` 内）覆盖分块布局推导、缺失块计算、时间戳格式化与 ID 校验。

## 实际运行记录（2026-09-24，Linux x86_64，Rust 1.98.1）

- `cargo test`：**17 个测试全部通过**（14 端到端 + 3 单元），`cargo clippy --all-targets` 无告警，`cargo fmt --check` 通过。
- `cargo build --release`：成功，二进制约 3.0 MiB。
- `examples/curl-demo.sh` 对 release 实跑（10 MiB 对象、3 块、乱序上传）：缺块 finish=400、
  乱序 201、同内容重传 200 `idempotent:true`、异内容 409、发布前 404、finish=200、
  重复 finish 幂等、下载长度与 SHA-256 与原文件完全一致、伪造整体哈希 finish=409 且对象 404。
- 手动崩溃演练：传完 chunk0 后 `SIGKILL`（kill -9）服务进程，同一 `DATA_DIR` 重启，
  会话状态中 chunk0 保留、`missing_chunks:[1]`、对象仍 404；续传 chunk1 → finish → 下载哈希一致。
- 运行环境说明：无数据库/外部服务；`MAX_CHUNK_SIZE` 为单块硬上限，块大小必须在创建会话时逐块声明。

## 未完成项 / 范围外

按题目“纯后端、HTTP 接口”范围，以下明确未做：

- 无前端/界面；无鉴权、TLS、限流与多用户隔离（任何人拿到 upload_id/object_id 即可访问）；
- 单实例、单机文件系统，无多副本/分布式；上传会话锁为进程内锁，多实例部署需换共享存储与分布式锁；
- 无过期会话/垃圾块的定时 GC（孤儿数据只在启动恢复时清理；已完成会话目录保留作为审计记录）；
- 无 HTTP Range 读取（对象只支持整体 GET/HEAD）；
- 整体哈希在 finish 时一次性串接重算（内存恒定 1 MiB，但会多读一遍全部数据，未做增量整体哈希）。

## 目录结构

```text
src/
  main.rs      入口（环境变量配置）
  lib.rs       应用装配
  api.rs       路由与处理器
  state.rs     共享状态、每会话互斥锁
  store.rs     存储层：会话/分块/发布/崩溃恢复（含单元测试）
  error.rs     统一错误类型
tests/api.rs   端到端测试（真实 HTTP、真实进程重启）
examples/      curl 演示脚本
```

## 已知边界与取舍

- 单实例、文件系统后端；无鉴权、无 TLS、无跨节点冗余（按题目“纯后端”范围）。
- 不自动发布：块齐但未 finish 的会话重启后仍需客户端显式 finish（崩溃恢复不会替客户端做提交决定）。
- finish 使用一次性整体重算哈希（按块流式读取，内存占用恒定为 1 MiB 缓冲）；同内容对象天然去重。
- `upload_id` 为服务端生成的 UUID v4；对象 ID 即其 SHA-256 十六进制（同时防路径穿越）。
