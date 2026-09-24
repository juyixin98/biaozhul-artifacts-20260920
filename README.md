# 构建来源证明验证服务（Build Provenance Verification）

纯后端服务：验证一份**构建来源证明（build provenance）**是否足以让某次构建产物准入。
证明包含**源提交、材料摘要、构建器标识和输出摘要**，由构建器用 **Ed25519** 私钥签名
（in-toto / SLSA v1 风格的陈述 + DSSE 信封）；验证服务依据策略**限定可信构建器和允许的
材料来源**，并对每条策略逐项给出判定。

技术栈：Rust + [Axum](https://github.com/tokio-rs/axum) 0.8，签名使用成熟库
[`ed25519-dalek`](https://crates.io/crates/ed25519-dalek) 2.x，摘要 SHA-256（`sha2`）。
**全部构建器身份（密钥）与材料均为仓库内本地夹具**，不访问任何外部服务。

---

## 1. 它验证什么

一封 JSON 信封（`fixtures/proofs/*.json`）结构：

```jsonc
{
  "payload_type": "application/vnd.in-toto+json; type=https://slsa.dev/provenance/v1",
  "payload": "<base64: 规范 JSON 陈述>",
  "signatures": [{ "keyid": "pkg:generic/ci-builder@v2", "sig": "<base64 Ed25519 签名>" }]
}
```

陈述（base64 解码后）为 in-toto Statement v1，谓词含：

| 字段 | 含义 |
|---|---|
| `predicate.builder_id` | 构建器标识（同时是签名公钥的 keyid） |
| `predicate.source_commit` | 源仓库 + 不可变提交哈希（40 位 hex） |
| `predicate.materials[]` | 材料 URI + `{sha256: ...}` 摘要 |
| `predicate.output` | 构建产物名称 + 输出摘要；与 Statement `subject` 绑定 |

签名覆盖 **DSSE PAE**：`"DSSEV1 " + payloadType + " " + len + " " + payload`，
payload 为按键排序、无空白的规范 JSON（字段书写顺序不影响验签）。

### 策略检查项（报告中逐条出现）

| # | check | 失败码示例 | 含义 |
|---|---|---|---|
| 1 | `signature` | `INVALID_SIGNATURE` | Ed25519 签名有效、keyid 已注册公钥、payload 可解析 |
| 2 | `subject_binding` | `SUBJECT_OUTPUT_MISMATCH` | Statement subject 与谓词 output 一致 |
| 3 | `trusted_builder` | `UNTRUSTED_BUILDER` | 构建器在本项目可信名单内 |
| 4 | `builder_binding` | `CROSS_BUILDER_REUSE` | 签名者 = 声明构建器 = **本次实际构建器**（防证明跨构建器复用） |
| 5 | `source_commit_binding` | `SOURCE_NOT_IN_MATERIALS` 等 | 源仓库在白名单、提交哈希不可变、源提交出现在材料中 |
| 6 | `materials_complete` | `MATERIAL_MISSING` / `MATERIAL_DIGEST_MISMATCH` | 每份声明材料都在本地材料库存在且摘要一致 |
| 7 | `material_sources_allowed` | `MATERIAL_SOURCE_DENIED` | 全部材料 URI 命中来源白名单前缀 |
| 8 | `output_digest` | `OUTPUT_DIGEST_MISMATCH` | 待准入产物的实际 SHA-256 与证明输出摘要一致 |

**准入条件：8 条全部通过**（报告 `accepted=true`，HTTP 200；否则 HTTP 422）。

---

## 2. 验收场景（本地夹具）

| 场景 | 夹具 | 关键判定 |
|---|---|---|
| 合法证明 | `proofs/valid.json` + 原产物 | 全部 PASS |
| **输出替换** | 同一证明 + `app-v1.0.0-tampered.tar.gz` | `output_digest=OUTPUT_DIGEST_MISMATCH` |
| **缺失材料** | 证明声明本地库没有的 `dep-b-0.9.5` | `materials_complete=MATERIAL_MISSING` |
| **跨构建器复用** | ci-builder 的真证明 + shadow-builder 的产物，`actual_builder_id` 如实上报 | `builder_binding=CROSS_BUILDER_REUSE`（输出摘要也不符） |
| 禁止材料来源（附加） | 依赖换为 `evil-mirror.example.test` | `material_sources_allowed=MATERIAL_SOURCE_DENIED` |
| 不可信构建器（附加） | shadow-builder 自签 | `trusted_builder=UNTRUSTED_BUILDER`（签名本身仍合法） |

> 跨构建器场景的关键安全属性：测试 `cross_builder_reuse_detected_even_with_identical_bytes`
> 证明——即使 B **逐字节复制** A 的产物（输出摘要相同），仅凭 `actual_builder_id` 与证明
> 构建器不符，复用证明也会被拒绝。

---

## 3. 启动命令与依赖

### 环境依赖

- Rust / Cargo（开发与实测版本：**rustc 1.98.1 / cargo 1.98.1，Linux x86_64**）；2021 edition
- 构建时需要访问 crates.io 拉取依赖（已提交 `Cargo.lock`，版本可复现）
- 运行时无外部依赖：无数据库、无网络调用，材料与密钥全部来自 `fixtures/`

直接依赖：`axum 0.8`、`tokio 1`、`serde / serde_json`、`ed25519-dalek 2`、
`sha2 0.10`、`base64 0.22`、`hex`、`anyhow`、`tracing / tracing-subscriber`；
开发依赖 `tower`（内存路由测试）、`tempfile`。

### 生成夹具 → 启动服务

```bash
# 1) 确定性生成 fixtures/（固定种子派生密钥；重复执行结果一致）
cargo run --bin gen-fixtures

# 2) 启动 HTTP 服务（参数均可省，默认 127.0.0.1:8080 和 ./fixtures）
cargo run --bin provenance-server -- 127.0.0.1:8080 fixtures
```

> 仓库已提交生成好的 `fixtures/`，可直接执行第 2 步。

### 跑测试 / 检查

```bash
cargo test       # 4 单元测试 + 7 HTTP 集成测试 + 9 策略场景测试 = 20 个全过
cargo clippy --all-targets
```

---

## 4. HTTP 接口

| 方法与路径 | 说明 |
|---|---|
| `GET /health` | 健康检查 |
| `GET /policy` | 当前策略：可信构建器、来源前缀、源仓库白名单、已注册 keyid、本地材料 URI |
| `POST /verify` | 提交信封 + 待验证产物，返回逐条判定报告 |
| `GET /scenarios` | 列出预置场景 |
| `POST /scenarios/{name}` | 一键复现预置场景（请求体取自 `fixtures/requests/`） |

### `POST /verify` 请求体

```jsonc
{
  // 证明来源二选一：
  "proof_path": "valid.json",        // fixtures/proofs/ 下相对路径（做目录穿越防护）
  // "envelope": { ... },            // 或直接内联完整信封

  // 待验证产物，三选一（服务端算 SHA-256）：
  "artifact_path": "app-v1.0.0.tar.gz", // fixtures/artifacts/ 下相对路径
  // "artifact_base64": "...",          // 或产物的 base64
  // "artifact_bytes": "...",           // 或原始文本（演示用）
  // "actual_output_digest": "<hex>",   // 或直接给实际摘要

  "actual_builder_id": "pkg:generic/ci-builder@v2" // 可选；实际执行构建者，用于跨构建器检测
}
```

### curl 样例（完整清单见 `examples/requests.md`，JSON 样例见 `examples/request-*.json`）

```bash
# 健康与策略
curl -s http://127.0.0.1:8080/health
curl -s http://127.0.0.1:8080/policy

# 合法证明 → 200, accepted=true
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' -d @examples/request-valid.json

# 输出替换 → 422, OUTPUT_DIGEST_MISMATCH
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' -d @examples/request-output-replaced.json

# 一键场景（其余场景 03 / 04 / 05 / 06 同理）
curl -s -X POST http://127.0.0.1:8080/scenarios/04-cross-builder-reuse
```

一把梭演示（含内联 envelope 与全部场景）：

```bash
./examples/curl-demo.sh
```

### 响应示例（输出替换，节选）

```json
{
  "accepted": false,
  "actual_output_digest": "…",
  "signer_keyid": "pkg:generic/ci-builder@v2",
  "checks": [
    { "check": "signature", "passed": true,  "code": "PASS", ... },
    ...
    { "check": "output_digest", "passed": false,
      "code": "OUTPUT_DIGEST_MISMATCH",
      "detail": "证明声称 5e91…，实际产物为 8c2a…：输出已被替换" }
  ],
  "statement": { ... }
}
```

---

## 5. 目录结构

```
Cargo.toml / Cargo.lock        # 依赖清单与锁定版本
src/
  main.rs                      # provenance-server 入口
  lib.rs
  types.rs                     # 陈述/信封/请求/报告模型，规范 JSON
  crypto.rs                    # Ed25519 签名/验签、SHA-256、DSSE PAE、密钥注册表
  policy.rs                    # 策略引擎：8 条检查逐条判定、本地材料库
  fixtures.rs                  # 加载 config.json（策略+公钥）与材料库
  server.rs                    # Axum 路由
  bin/gen-fixtures.rs          # 确定性生成全部夹具
fixtures/
  config.json                  # 可信构建器、白名单、构建器公钥（hex）
  materials/                   # 本地材料 + MANIFEST.json（URI→文件→启动时算摘要）
  artifacts/                   # 原产物 / 被替换产物 / 影子构建器产物
  proofs/*.json                # 六份签名证明信封
  requests/*.json              # 六个场景的 /verify 请求样例
tests/                         # api.rs（HTTP）、policy.rs（场景）、common/
examples/                      # curl 样例与请求 JSON
docs/demo-output.txt           # 实际端到端运行输出存档
docs/test-output.txt           # 实际测试输出存档
```

---

## 6. 实际运行结果（2026-09-24，本机如实记录）

- `cargo build`：通过，无警告；`cargo clippy --all-targets`：无警告。
- `cargo test`：**20/20 通过**（4 单元 + 7 HTTP + 9 策略），逐名输出见 `docs/test-output.txt`。
- 启动服务后 `./examples/curl-demo.sh`：六个场景逐条判定与第 2 节预期完全一致，
  完整输出见 `docs/demo-output.txt`：
  - 合法 200/全 PASS；输出替换仅 `OUTPUT_DIGEST_MISMATCH`；
    缺失材料仅 `MATERIAL_MISSING`；跨构建器复用为 `CROSS_BUILDER_REUSE`；
    禁止来源为 `MATERIAL_SOURCE_DENIED`；不可信构建器为 `UNTRUSTED_BUILDER`。
- 另手工验证：篡改 envelope payload → `INVALID_SIGNATURE`；伪造/未知 keyid →
  `INVALID_SIGNATURE`；`proof_path`/`artifact_path` 目录穿越 → 400。

## 7. 范围说明与未完成项

本项目是**后端验证原型**，刻意不做的事情：

- 无前端界面（按要求仅纯后端 + HTTP 接口与样例）。
- 材料不从远端拉取：本地 `MaterialStore` 模拟“验证者已取得材料”，
  `MANIFEST.json` 仅承担 URI→本地文件映射。
- 夹具密钥由**固定种子**确定性派生（仅为可复现）；生产环境应使用随机密钥与
  KMS/HSM、密钥轮换与撤销机制，公钥分发应有带外信任根。
- 策略为静态单项目策略（`fixtures/config.json`）；未做多租户、策略版本化、
  时间戳/有效期（freshness）、吊销列表（Rekor 之类透明日志）或多签名门槛。
- `actual_builder_id` 假定由可信准入环境如实上报；其与证明的联动在真实系统中
  通常由编排平台（如带证明的 CI 运行者身份）提供，本原型不解决该信任引导问题。
- 未做速率限制、鉴权与审计持久化（演示服务，监听本机回环为宜）。
