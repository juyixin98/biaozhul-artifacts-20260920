# 远端构建缓存原型（Remote Build Cache Prototype）

用 Rust + Axum 实现的**纯后端**构建缓存服务，核心模型对齐 Bazel 远程执行 API
（[REAPI v2](https://github.com/bazelbuild/remote-apis)）的 Action Cache (AC) 与
Content Addressable Storage (CAS)：

- **动作摘要映射不可变输出清单**：`Action`（命令 + 输入根 + 平台/环境）经规范化 JSON
  序列化后取 SHA-256，作为动作缓存键，值为不可变的 `ActionResult`（输出清单）。
- **输出块内容寻址**：所有输出文件/stdout/stderr 作为不可变 blob 存入 CAS，
  键即内容的 SHA-256；清单只保存摘要引用。
- **先验证、后发布**：发布动作结果前，服务端对清单引用的**每个**对象做
  「存在性 + 长度 + 全量 SHA-256」核验，全部通过才原子落盘 AC；任一缺块/损坏
  一律返回错误，**绝不产生动作记录，绝不返回伪成功**。
- **命中即复验**：查询缓存命中时再次核验清单引用的全部对象；对象丢失或位腐烂时
  返回 `503 cache_corruption`，而不是把不可信结果交给客户端。

无界面，仅 HTTP/JSON 接口。

## 依赖

- Rust（开发与验证使用 `cargo 1.98.1 / rustc 1.98.1`，edition 2021）
- 主要 crate：`axum 0.7`、`tokio 1`、`serde / serde_json 1`、`sha2 0.10`、
  `hex`、`tower 0.5`、`tracing`
- 构建/运行不需要外部数据库或系统服务；缓存数据保存在本地文件系统目录。
- 版本锁定见 `Cargo.lock`（已提交）。

## 启动

```bash
# 开发模式
cargo run --bin rbc-server -- --addr 127.0.0.1:8080 --store ./cache-data

# 或 release
cargo build --release
./target/release/rbc-server --addr 127.0.0.1:8080 --store ./cache-data

# 也可用环境变量（命令行参数优先）
RBC_ADDR=127.0.0.1:8080 RBC_STORE=./cache-data RUST_LOG=info ./target/release/rbc-server
```

参数：

| 参数 | 环境变量 | 默认值 | 说明 |
|---|---|---|---|
| `--addr` | `RBC_ADDR` | `127.0.0.1:8080` | 监听地址 |
| `--store` | `RBC_STORE` | `./cache-data` | 缓存数据目录（自动创建） |
| `--max-blob-bytes` | `RBC_MAX_BLOB_BYTES` | `16777216`（16 MiB） | 单请求体上限 |

健康检查：

```bash
curl -sS http://127.0.0.1:8080/healthz   # -> ok
```

## HTTP 接口

摘要（Digest）统一表示为：

```json
{ "hash": "<64位小写十六进制 SHA-256>", "size_bytes": 123 }
```

| 方法 | 路径 | 说明 | 成功 | 典型失败 |
|---|---|---|---|---|
| `GET` | `/healthz` | 健康检查 | 200 | — |
| `PUT` | `/blobs/:hash?size_bytes=N` | 上传 CAS 块（body 为原始字节），服务端全量校验哈希与长度 | 200 | 400 哈希/长度不符 |
| `GET` | `/blobs/:hash` | 下载块，返回前全量复验；可带 `X-Expected-Size-Bytes` 交叉校验 | 200 | 404 不存在；503 内容损坏 |
| `HEAD` | `/blobs/:hash` | 块是否存在且完整（损坏视为不存在） | 200/404 | — |
| `POST` | `/find-missing` | 批量查询缺失/损坏摘要（去重） | 200 | 400 摘要非法 |
| `PUT` | `/actions/:hash` | 发布动作结果；`:hash` 必须等于 body 中 action 的规范摘要 | 200 | 424 引用块缺失；400 动作键不符；409 同键不同结果 |
| `GET` | `/actions/:hash` | 查询动作结果（命中前复验全部引用对象） | 200 `HIT` | 404 未命中；503 引用对象损坏/缺失 |
| `POST` | `/util/action-digest` | 原型辅助：返回 action 的规范 JSON 与摘要 | 200 | 400 |

错误响应统一为：

```json
{ "error": "missing_blobs", "message": "1 referenced blob(s) missing from CAS: ..." }
```

### 客户端典型流程（缓存命中 vs 重新执行）

```
1. 构造 Action ──POST /util/action-digest（或本地计算）──> action_key
2. GET /actions/<action_key>
     ├─ 200 HIT：直接从 CAS 下载清单中的输出块（GET 时服务端复验），跳过执行
     └─ 404 cache_miss / 503 corruption：重新执行动作
3. 重新执行后：
   a. PUT /blobs/<h>?size_bytes=N  上传每个输出块（也可先 POST /find-missing 跳过已有块）
   b. PUT /actions/<action_key>    发布输出清单
        └─ 200 仅当全部引用对象核验通过；缺块返回 424，AC 中不会留下记录
```

## 请求样例（curl）

可直接运行 `bash examples/requests.sh`（脚本会启动/复用本机 8080 端口的服务；
或先自行启动服务后运行 `BASE=http://127.0.0.1:8080 bash examples/requests.sh`）。

```bash
BASE=http://127.0.0.1:8080

# 1) 一个“动作”：gcc 编译 main.c
ACTION='{"arguments":["gcc","-c","main.c","-o","main.o"],
         "platform":{"cpu":"x86_64","os":"linux"},
         "working_directory":"."}'

# 2) 取动作的规范摘要（真实客户端应本地计算；此处仅为手工调试便利）
AH=$(curl -sS -X POST $BASE/util/action-digest \
      -H 'content-type: application/json' -d "$ACTION" | jq -r .hash)

# 3) 发布前查询 → 404 cache_miss（需要重新执行）
curl -sS -i $BASE/actions/$AH

# 4) “重新执行”后上传输出块（内容寻址；哈希不符会被 400 拒绝）
printf '<<< fake main.o >>>' > main.o
BH=$(sha256sum main.o | cut -d' ' -f1)
N=$(stat -c%s main.o)
curl -sS -X PUT "$BASE/blobs/$BH?size_bytes=$N" --data-binary @main.o

# 5) 先查缺块（批量、去重；损坏对象也会报为缺失）
curl -sS -X POST $BASE/find-missing -H 'content-type: application/json' \
  -d "{\"digests\":[{\"hash\":\"$BH\",\"size_bytes\":$N}]}"

# 6) 发布动作结果 —— 全部对象核验通过才落盘
curl -sS -X PUT $BASE/actions/$AH -H 'content-type: application/json' \
  -d "{\"action\":$ACTION,
       \"action_result\":{
         \"exit_code\":0,
         \"stdout_raw\":\"build ok\n\",
         \"output_files\":[{\"path\":\"main.o\",
                           \"digest\":{\"hash\":\"$BH\",\"size_bytes\":$N},
                           \"is_executable\":false}]}}"

# 7) 再次查询 → 200 HIT
curl -sS $BASE/actions/$AH | jq .

# 8) 下载输出块（服务端返回前复验哈希；可带 X-Expected-Size-Bytes 交叉校验）
curl -sS $BASE/blobs/$BH -o downloaded.o
sha256sum downloaded.o   # 必须等于 $BH
```

Rust 端到端示例（内置极简 HTTP 客户端，无额外依赖）：

```bash
cargo run --bin rbc-server -- --store ./demo-cache        # 终端 A
cargo run --example demo                                  # 终端 B
```

## 验收场景与对应自动化测试

`cargo test`（共 18 个测试：5 个单元测试 + 13 个 HTTP 端到端集成测试）：

| 验收要求 | 测试 |
|---|---|
| 缓存命中路径 | `miss_then_publish_then_hit`（404 → 发布 → 200 HIT） |
| 重新执行路径 | 同上；`GET` 404 `cache_miss` 明确提示 client must re-execute |
| 缺块发布不得伪成功 | `publish_with_missing_blob_is_rejected`（424，且 AC 无记录） |
| 嵌套目录/stdout 块缺失 | `missing_blob_in_nested_directory_and_stdout_is_rejected` |
| 并发缺块发布 | `concurrent_publish_while_blob_missing_all_fail_and_nothing_published`（12 并发全部 424，无记录） |
| 缓存对象损坏（下载） | `corrupted_blob_download_is_rejected`（GET 503、HEAD 404、find-missing 报缺失） |
| 损坏不得伪命中 | `corrupted_referenced_blob_breaks_action_hit`（GET action 503；悬空引用同样 503） |
| 损坏可自愈 | `corrupted_blob_download_is_rejected` 末尾：重新上传正确内容后恢复 |
| 并发上传相同块 | `concurrent_identical_blob_uploads_succeed_once_consistent`（16 并发全 200，内容一致） |
| 并发上传相同动作 | `concurrent_identical_action_publishes_all_agree`（12 并发全 200，幂等一致） |
| 哈希/长度不符拒绝 | `blob_upload_with_wrong_hash_is_rejected_and_not_stored` |
| 动作键与内容不符 | `publish_rejects_action_key_mismatch` |
| 结果不可变 | `action_result_is_immutable_and_action_blob_stored`（同键不同结果 409） |
| 下载交叉校验长度 | `download_with_expected_size_cross_check` |
| 缺失去重 | `find_missing_reports_only_absent_and_dedups` |

运行：

```bash
cargo test
```

## 存储布局

```
<store>/
├── cas/aa/bb/<64hex>   # 内容块，两级分桶；键即 SHA-256(content)
├── tmp/                # 上传临时文件，rename 原子发布
└── ac/<64hex>.json     # 动作结果（规范 JSON），发布后不可变；另有 ac/tmp/
```

并发与原子性要点：

- 上传：写临时文件 → `rename(2)` 原子落盘；同哈希上传经每键异步互斥锁串行化，
  已存在对象先读盘复验，内容损坏时用已验证的新内容原子替换（自愈）。
- 发布：先把 action 自身写入 CAS，再逐个全量核验清单引用对象，最后原子写 AC；
  同动作键并发发布串行化，相同内容幂等 200，不同内容 409。
- 读取：每次都从磁盘重新全量校验，不信任任何缓存元数据。

## 设计取舍与未完成项（原型边界）

- **无真正的 Merkle 输入根实现**：`Action.input_root_digest` 为不透明摘要，
  由调用方自行计算并保证对象已上传；本原型聚焦 AC/CAS 的发布与核验语义。
- **无鉴权 / TLS / 压缩 / 分块上传 / 断点续传**；单请求体受
  `--max-blob-bytes` 限制（默认 16 MiB），blob 下载为整文件读入后复验。
- **单机本地存储、单实例**：锁在进程内；未做多副本、GC 与引用计数
  （当前 blob 永不回收）、按租户配额。
- Action 与 ActionResult 采用规范 JSON 编码（而非 REAPI 的 protobuf），
  语义一致但线路格式不与 REAPI 客户端互通；字段名与概念保持对应，便于对照。
- 头 `X-Expected-Size-Bytes` 为原型自定义的下载长度交叉校验手段。
