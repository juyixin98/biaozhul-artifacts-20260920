# 制品分阶段晋级服务（Artifact Promotion）

纯后端 HTTP 服务，用 Rust + [Axum](https://github.com/tokio-rs/axum) 实现测试制品的
**分阶段晋级状态机**：

```
development ──promote──▶ validation ──promote(审批+必要证明)──▶ release
     ▲                        │
     └────── rollback ────────┴──────── rollback ──────── release
                                  （只回退一级，可逐级回退）
```

## 核心不变量（验收点）

1. **同一不可变摘要绑定全生命周期**
   - 制品注册时由**服务端**对请求体计算 SHA-256（客户端无法伪造），得到 `sha256:<hex>`。
   - 制品在 development / validation / release 三个阶段绑定的是**同一个**摘要，晋级、回退均不改变。
   - 注册后内容不可替换：`POST /artifacts/{id}/content/immutable` 一律 `409`；
     内容变了只能注册为新制品（内容寻址存储 CAS，同摘要内容只存一份）。
2. **测试证明必须引用该摘要**
   - 登记证明时 `digest` 必须与制品绑定摘要一致，否则 `409`；格式非法 `400`。
   - 发布门禁要求每一种“必要证明”都存在一条 `passed=true` 且摘要匹配的记录。
3. **回退保留历史且不重新构建产物**
   - 回退/晋级只移动阶段指针并向**只追加事件流**写入一条事件，绝不触碰 blob；
     `build_count` 不增加，内容字节逐字不变（响应头 `X-Content-Digest` 回带摘要）。
4. **审批与门禁**
   - 晋级到 `release` 必须 `approved=true` 且 `approver` 非空，否则 `422`/`400`。
   - 缺少任一必要证明（或对应证明 `passed=false`）不能发布，返回 `422` 并列出缺失项。
5. **并发安全**
   - 所有状态变更在同一把锁内裁决：重复晋级、并发回退中只有一个请求成功，其余 `409`。

## 依赖与环境

- Rust（开发与验证使用 `rustc/cargo 1.98.1`，edition 2021）；无系统级依赖，无需数据库。
- 关键 crate（版本锁定在 `Cargo.lock`）：
  `axum 0.8`、`tokio 1`、`serde 1`、`serde_json 1`、`sha2 0.10`、`hex 0.4`、
  `uuid 1`、`thiserror 1`、`tracing 0.1`、`tracing-subscriber 0.3`；
  测试用 `tower 0.5`（dev-dependency）。

## 启动命令

```bash
# 1. 拉取并锁定依赖（生成 Cargo.lock），编译
cargo build --release

# 2. 启动（默认监听 127.0.0.1:8080）
./target/release/artifact-promotion

# 可选环境变量
BIND_ADDR=0.0.0.0:8080 \
REQUIRED_PROOF_KINDS=unit-test,integration-test,security-scan \
  ./target/release/artifact-promotion
```

开发模式可直接 `cargo run`。健康检查：`GET /health` → `{"status":"ok"}`。

> 数据为**进程内存**存储，重启即清空（本任务范围为纯后端状态机与接口；持久化见“未完成项”）。

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/artifacts` | 注册制品。请求体为**原始制品字节**（`application/octet-stream`）；服务端计算并绑定摘要，阶段初始为 `development`。`201` |
| GET | `/artifacts` | 列出全部制品 |
| GET | `/artifacts/{id}` | 查询单个制品 |
| GET | `/artifacts/{id}/history` | 只追加的事件历史 |
| GET | `/artifacts/{id}/content` | 取回原始字节；响应头 `X-Content-Digest` |
| POST | `/artifacts/{id}/proofs` | 登记测试/扫描证明（JSON） |
| POST | `/artifacts/{id}/promote` | 晋级一级（JSON，可带审批字段） |
| POST | `/artifacts/{id}/rollback` | 回退一级（JSON，可带 `reason`） |
| POST | `/artifacts/{id}/content/immutable` | 内容替换入口——**恒被拒绝 `409`**，用于显式验证不可变性 |
| GET | `/blobs/{digest}` | 查询 CAS 中 blob 的引用计数等元数据 |
| GET | `/health` | 健康检查 |

请求体 JSON 字段：

- 证明 `POST /proofs`：`{"kind":"unit-test","digest":"sha256:<hex>","passed":true,"detail":"..."}`
  （`digest` 必填且必须等于绑定摘要；`passed` 缺省为 `false`）
- 晋级 `POST /promote`：`{"approved":true,"approver":"alice"}`
  （development→validation 不校验审批；validation→release 必须审批且证明齐全）
- 回退 `POST /rollback`：`{"reason":"hotfix"}`（可空）

状态码约定：`200/201` 成功；`400` 请求字段非法；`404` 制品不存在；
`409` 状态机冲突（重复晋级、非法回退、摘要不匹配、内容不可替换）；
`422` 门禁未满足（未审批、缺必要证明）。错误体形如
`{"error":"precondition_failed","message":"..."}`。

## 请求样例（curl）

以下脚本也保存在 `examples/demo.sh`（需要 `curl`，可选 `jq`/`python3` 仅用于美化）：

```bash
BASE=http://127.0.0.1:8080

# 1) 注册（体内容即制品字节，摘要由服务端计算）
curl -s -X POST "$BASE/artifacts" \
  -H 'content-type: application/octet-stream' \
  --data-binary 'release-bundle-v1.2.3'
# -> 201 {"id":"...","digest":"sha256:1a85...","stage":"development","build_count":1,...}
# 可用独立工具核对：printf 'release-bundle-v1.2.3' | sha256sum

ID=...   # 用上一步返回的 id
DIGEST=sha256:...

# 2) 登记三条引用该摘要的通过证明
for K in unit-test integration-test security-scan; do
  curl -s -X POST "$BASE/artifacts/$ID/proofs" -H 'content-type: application/json' \
    -d "{\"kind\":\"$K\",\"digest\":\"$DIGEST\",\"passed\":true,\"detail\":\"$K ok\"}"
done

# 3) development -> validation（无需审批）
curl -s -X POST "$BASE/artifacts/$ID/promote" \
  -H 'content-type: application/json' -d '{}'

# 4) 审批后 validation -> release
curl -s -X POST "$BASE/artifacts/$ID/promote" \
  -H 'content-type: application/json' \
  -d '{"approved":true,"approver":"alice"}'

# 5) 反例：引用别的摘要的证明 -> 409
curl -s -X POST "$BASE/artifacts/$ID/proofs" -H 'content-type: application/json' \
  -d "{\"kind\":\"unit-test\",\"digest\":\"sha256:$(printf evil|sha256sum|cut -d' ' -f1)\",\"passed\":true}"

# 6) 反例：审批后替换内容 -> 409
curl -s -X POST "$BASE/artifacts/$ID/content/immutable" \
  -H 'content-type: application/octet-stream' --data-binary 'tampered'

# 7) 回退（保留历史、不重建）
curl -s -X POST "$BASE/artifacts/$ID/rollback" \
  -H 'content-type: application/json' -d '{"reason":"hotfix regression"}'
curl -s "$BASE/artifacts/$ID/history"
curl -s -D - "$BASE/artifacts/$ID/content" -o /tmp/out   # 头里有 X-Content-Digest

# 8) 并发回退（只有一个能成功，其余 409）
for i in $(seq 1 10); do
  curl -s -o /dev/null -w '%{http_code}\n' -X POST "$BASE/artifacts/$ID/rollback" \
    -H 'content-type: application/json' -d "{\"reason\":\"race-$i\"}" &
done | sort | uniq -c
```

## 自动化测试

```bash
cargo test
```

- 单元测试（`src/`）：阶段状态机方向、SHA-256 标准向量与摘要规范化。
- 端到端 HTTP 测试（`tests/api.rs`，通过 `tower::ServiceExt::oneshot` 直连 Router）：
  - `happy_path_development_to_release`：注册→证明→两阶段晋级→重复晋级 409；
  - `release_gate_requires_approval_and_passing_proofs`：未审批、审批人为空、缺证明、
    存在失败证明均不能发布，补齐后才能发布；
  - `content_is_immutable_after_registration`：**审批后**替换内容仍 409，字节逐字不变；
  - `proof_must_reference_bound_digest`：证明引用错误摘要 409，非法格式 400；
  - `concurrent_duplicate_promotion_only_one_succeeds`：6 并发晋级，恰 1 个 200；
  - `concurrent_rollback_only_one_succeeds_and_history_is_kept`：5 并发回退，恰 1 个 200、
    其余 409；摘要/内容/`build_count` 不变，事件历史完整，回退后可重新晋级；
  - `content_addressed_storage_is_not_rebuilt`：同内容重复注册引用同一份 blob；
  - `invalid_json_and_bad_paths_are_handled`：非法 JSON/缺字段/404/健康检查。

## 实际运行结果（本仓库已验证）

- `cargo build --release`：成功；`cargo clippy --all-targets`：无 warning/error。
- `cargo test`：**14/14 通过**（6 单元 + 8 端到端）。
- release 二进制真实启动后用 curl 全流程验证：
  服务端摘要与 `sha256sum` 独立计算一致；错误摘要证明 409；缺证明/未审批发布 422；
  审批+三证明发布 200；重复晋级 409；审批后替换内容 409；回退后内容与摘要不变、
  `build_count` 仍为 1；历史只追加；10 个**真实 HTTP 并发**回退恰为 `1×200 + 9×409`，
  回退事件仅 1 条。

## 目录结构

```
Cargo.toml / Cargo.lock     # 依赖清单与锁定版本
src/
  main.rs                   # 入口（BIND_ADDR / REQUIRED_PROOF_KINDS）
  lib.rs                    # 模块导出
  app.rs                    # Axum 路由与处理器
  store.rs                  # 内存 CAS + 状态机（晋级/回退/门禁/并发裁决）
  models.rs                 # Stage / Event / 请求与响应类型
  digest.rs                 # SHA-256 计算与摘要规范化
  error.rs                  # 统一错误 -> HTTP 状态码
tests/api.rs                # 端到端 HTTP 测试
examples/demo.sh            # curl 演示脚本
```

## 设计说明与未完成项

- **为什么摘要由服务端计算**：若接受客户端自报摘要，调用方可给篡改内容配旧摘要，
  破坏“同一不可变摘要”语义；服务端对原始字节计算后绑定，证明只能引用该值。
- **回退语义**：回退一级（release→validation→development），不删除任何事件与证明；
  回退后可重新晋级。回退**不重建内容**——blob 与摘要完全不变。
- **并发**：采用单 `Mutex` 临界区裁决（操作均为内存级、临界区极短）。
  这对本任务足够且可证明正确；高吞吐场景可换成按制品分片锁或持久化事务。
- **未完成 / 超出本次范围**：
  1. 无持久化，进程重启数据丢失（接入 SQLite/Postgres 或 WAL 事件溯源是自然下一步）；
  2. 无认证/鉴权与审批人身份体系（`approver` 目前为自由文本）；
  3. 无分页/过滤、指标与 OpenAPI 描述文件；
  4. CAS 当前无垃圾回收（制品删除接口未提供）；
  5. 阶段集合固定为三级，暂不支持自定义流水线。
