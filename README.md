# 远端构建缓存原型（Rust + Axum）

纯后端 HTTP 构建缓存：动作摘要映射不可变输出清单（Action Cache, AC），输出块按内容寻址（Content-Addressable Storage, CAS）。只有清单引用的全部对象核验通过才发布动作结果；下载与命中时重新校验摘要，缓存对象损坏绝不返回伪成功。

## 设计

```
客户端                          服务端
  |-- PUT /cas/<sha256> ------->| 校验 body 的 sha256 == 路径摘要，不符 400；
  |                             | 原子写（tmp + rename），并发上传同块幂等
  |-- PUT /ac/<action_digest> ->| 逐一核验清单引用的全部 CAS 对象
  |   (JSON 输出清单)           | （存在 + 重新哈希），任一缺失/损坏 -> 422，不落盘；
  |                             | 全部通过才原子发布；同动作重复发布内容必须一致，
  |                             | 一致幂等 200，不一致 409（不可变）
  |-- GET /ac/<action_digest> ->| 未命中 404（客户端应重新执行）；
  |                             | 命中前再次核验全部对象，损坏 -> 500，不返回伪成功
  |-- GET /cas/<sha256> ------->| 读出后重新哈希校验，损坏 -> 500
```

- 动作摘要由客户端计算（如 `sha256(命令 + 输入)`），服务端只认 64 位小写十六进制。
- 存储布局：`DATA_DIR/cas/<前2位>/<sha256>`、`DATA_DIR/ac/<action_digest>.json`。
- 所有写入均为「临时文件 + rename」，并发上传相同动作/相同块安全且幂等。

## 依赖与启动

依赖：Rust 1.70+（开发用 1.98.1）、cargo。第三方 crate 见 `Cargo.toml`，版本已锁定在 `Cargo.lock`（axum 0.7、tokio 1、sha2、serde 等；测试用 reqwest + rustls，无系统 OpenSSL 依赖）。

```bash
cargo build --release
DATA_DIR=./data BIND=127.0.0.1:8080 ./target/release/build-cache
# 或开发模式： cargo run
```

环境变量：`DATA_DIR`（默认 `./data`）、`BIND`（默认 `127.0.0.1:8080`）、`RUST_LOG`（默认 `info`）。

## HTTP 接口与请求样例

```bash
# 上传输出块（摘要必须与内容一致，否则 400）
D=$(sha256sum hello.o | cut -d' ' -f1)
curl -X PUT --data-binary @hello.o http://127.0.0.1:8080/cas/$D        # 201 / 200(已存在)

# 查询块是否存在
curl -I http://127.0.0.1:8080/cas/$D                                   # 200 / 404

# 下载块（服务端重新校验摘要，损坏返回 500）
curl -O http://127.0.0.1:8080/cas/$D

# 发布动作结果（全部对象核验通过才落盘，否则 422 且不留记录）
A=$(printf 'gcc -c hello.c<inputs>' | sha256sum | cut -d' ' -f1)
curl -X PUT -H 'content-type: application/json' \
  -d "{\"exit_code\":0,\"outputs\":[{\"path\":\"hello.o\",\"digest\":\"$D\",\"size\":60}]}" \
  http://127.0.0.1:8080/ac/$A                                          # 201 / 200 / 409 / 422

# 查询动作缓存（未命中 404 -> 重新执行；命中 200 返回清单）
curl http://127.0.0.1:8080/ac/$A
```

错误响应统一为 JSON：`{"error": "...", "missing": [...], "corrupt": [...]}`。

## 测试与演示

```bash
cargo test                 # 7 个集成测试（真实起服务、真实 HTTP 请求）
./examples/demo.sh         # 端到端演示（需先启动服务；DATA_DIR 指向服务端数据目录可演示损坏场景）
```

测试覆盖（`tests/api.rs`）：

| 测试 | 验收点 |
|---|---|
| `upload_download_roundtrip` | 上传/下载回环、重复上传幂等 |
| `upload_with_wrong_digest_rejected` | 摘要不符 400 且不留存 |
| `publish_with_missing_blob_rejected_and_not_stored` | 缺块发布 422，AC 不留记录 |
| `publish_then_cache_hit_and_miss` | 未命中(404)->重执行->发布->命中(200) |
| `corrupt_cas_object_fails_download_and_action_hit` | 篡改磁盘对象后下载/命中/重发布均报错 |
| `concurrent_uploads_same_blob_and_action` | 16 路并发上传同块、并发发布同动作全部成功且结果一致 |
| `conflicting_publish_same_action_rejected` | 同动作不同清单 409，缓存保持首次内容 |

## 实测记录（2026-09-24，本机 Linux 6.8，rustc 1.98.1）

- `cargo test`：**7 passed; 0 failed**（另 doc-tests 0 个）。
- `./examples/demo.sh` 对 `127.0.0.1:18080` 实跑：未命中 404 → 上传 201 → 缺块发布 422 且 AC 仍 404 → 发布 201 → 命中 200 → 下载摘要一致 → 篡改对象后 GET /cas 与 GET /ac 均 500 → 恢复后 200。全部符合预期。

## 未完成项 / 已知限制

- 单节点本地磁盘存储，无分布式、无容量管理与 LRU 淘汰。
- 无认证/授权与 TLS（原型假定内网可信环境）。
- 核验在请求路径上同步全量读盘哈希，大对象场景应改流式 + 后台校验。
- 清单未包含 stdout/stderr 摘要与平台属性，动作摘要的规范化（输入树哈希）留给客户端。
- 损坏对象仅报错，未做自动隔离/修复。
