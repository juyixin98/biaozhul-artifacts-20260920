# 构建来源证明验证服务（Build Provenance Verifier）

用 Rust + Axum 实现的**纯后端**构建来源证明（SLSA / in-toto 风格）验证服务。
验证方提交一份由构建器签名的**证明信封**以及自己期望的源提交、材料和输出摘要，
服务按策略逐条判定，返回结构化验证报告。

证明包含：

| 组成 | 字段 |
|---|---|
| 源提交 | `source_commit.repo` / `source_commit.revision` |
| 材料摘要 | `materials[].uri` + `materials[].digest`（sha256） |
| 构建器标识 | `builder_id` |
| 输出摘要 | `output.digest`（sha256） |
| 签名 | `key_id`（签名公钥 hex）+ `signature_hex`（Ed25519，对规范化 JSON 签名） |

签名使用成熟库 [`ed25519-dalek`](https://crates.io/crates/ed25519-dalek)
（RustCrypto 维护），摘要使用 `sha2`。

策略限定：

- **可信构建器注册表**：`builder_id` 必须在 `policy.json` 中，且信封公钥必须
  与该构建器的注册公钥完全一致；
- **允许的源仓库**：`source_commit.repo` 必须在允许列表，且提交与验证方期望一致；
- **允许的材料来源**：每项材料 URI 必须命中允许的来源前缀；
- 此外还检查材料集合的**完整性/摘要一致性**、输出摘要比对与签名有效性。

## 依赖

- Rust（开发与验证环境：`rustc 1.98.1` / `cargo 1.98.1`，edition 2021）
- 直接依赖（版本以 `Cargo.lock` 为准）：
  `axum 0.8`、`ed25519-dalek 2`、`sha2 0.10`、`serde 1`、`serde_json 1`、
  `hex 0.4`、`tokio 1`
- 仅需网络一次以下载 crate；运行时无任何外部依赖（无数据库、无外部 PKI）。
  构建器身份、材料内容全部是**确定性本地夹具**。

## 启动命令

```bash
# 1. 生成本地夹具（首次可省略——服务启动时若目录缺失会自动生成）
cargo run --bin gen-fixtures -- fixtures

# 2. 启动服务（默认 127.0.0.1:8080，夹具目录 ./fixtures）
cargo run --bin provenance-server

# 可选环境变量：
#   PROVENANCE_LISTEN_ADDR=0.0.0.0:9000
#   PROVENANCE_FIXTURES_DIR=/path/to/fixtures
```

一键演示（生成夹具 + 构建 + 启动 + 依次请求四个验收场景）：

```bash
./run_demo.sh
```

## HTTP 接口

| 方法/路径 | 说明 |
|---|---|
| `GET /healthz` | 存活探针 |
| `GET /` | 接口说明 |
| `POST /v1/verify` | 验证证明，返回 `VerificationReport` |
| `POST /v1/fixture-sign` | **仅用于本地演示**：用夹具中的构建器私钥对 payload 签名 |

### `POST /v1/verify` 请求格式

```json
{
  "envelope": {
    "payload": {
      "builder_id": "trusted-builder-a",
      "source_commit": { "repo": "https://git.example.com/app/payments",
                          "revision": "7b3f9c1e…" },
      "materials": [
        { "uri": "https://deps.example.com/distfiles/serde-1.0.tar.gz",
          "digest": { "alg": "sha256", "hex": "71945515…" } }
      ],
      "output": { "digest": { "alg": "sha256", "hex": "97ce9bec…" } }
    },
    "key_id": "de3ef197…（签名者 Ed25519 公钥 hex）",
    "signature_hex": "…（对 payload 规范化字节的 Ed25519 签名 hex）"
  },
  "expected_source_commit": { "repo": "…", "revision": "…" },
  "expected_materials": [ { "uri": "…", "digest": { "alg": "sha256", "hex": "…" } } ],
  "expected_output": { "digest": { "alg": "sha256", "hex": "…" } }
}
```

响应（HTTP 恒为 200，策略结论在 `verdict`；请求体非法才是 400）：

```json
{
  "verdict": "ALLOWED",
  "builder_id": "trusted-builder-a",
  "checks": [
    { "id": "signature", "name": "Ed25519 签名验证通过",
      "status": "PASS", "detail": "…" }
  ]
}
```

### curl 样例

```bash
# 合法场景
curl -sS -X POST http://127.0.0.1:8080/v1/verify \
  -H 'content-type: application/json' \
  --data-binary @fixtures/examples/01_valid.json

# 输出替换 / 缺失材料 / 跨构建器复用
curl -sS -X POST http://127.0.0.1:8080/v1/verify -H 'content-type: application/json' \
  --data-binary @fixtures/examples/02_output_substituted.json
curl -sS -X POST http://127.0.0.1:8080/v1/verify -H 'content-type: application/json' \
  --data-binary @fixtures/examples/03_material_missing.json
curl -sS -X POST http://127.0.0.1:8080/v1/verify -H 'content-type: application/json' \
  --data-binary @fixtures/examples/04_cross_builder_reuse.json
```

四份可直接发送的请求样例在 `fixtures/examples/`（由 `gen-fixtures` 生成，
已随仓库提交）。

## 八条策略检查点

按执行顺序（全部跑完，不因一次失败而短路，报告同时呈现所有证据）：

1. `digest_algorithms` — 所有摘要使用受支持算法（sha256）
2. `trusted_builder` — `builder_id` 在可信注册表
3. `signing_key_binding` — 信封公钥 == 注册表中该构建器的公钥
4. `source_commit` — 源仓库在允许列表且源提交与期望一致
5. `material_origins` — 每项材料 URI 命中允许来源前缀
6. `materials_complete` — 证明材料集合与期望集合逐 URI、逐摘要一致
   （区分**缺失** / **摘要被改** / **多余材料**）
7. `output_match` — 证明输出摘要与消费者实际制品摘要一致
8. `signature` — Ed25519 签名对规范化 JSON 验证通过

任一 FAIL 则总判定 `DENY`，全部 PASS 为 `ALLOWED`。

## 验收场景与实测判定

| 场景 | 构造方式 | 拒绝的检查点 | 实测总判定 |
|---|---|---|---|
| 01 合法基线 | 可信构建器 A 签名，4 项材料与输出均吻合 | —（8/8 PASS） | `ALLOWED` |
| 02 **输出替换** | 证明合法且签名有效，但消费者实际制品被换成后门版本 | `output_match` FAIL（证明摘要 ≠ 实际制品） | `DENY` |
| 03 **缺失材料** | A 合法签名，但证明只登记 3 项材料，漏掉 `acme-sdk-5.0` | `materials_complete` FAIL（证明缺失材料：…） | `DENY` |
| 04 **跨构建器复用** | 证明主体与合法场景逐字节相同，但被 rogue 构建器用自己的密钥重签 | `signing_key_binding` FAIL（信封公钥 ≠ A 的注册公钥）；`signature` 本身 PASS，证明签名自洽但身份绑定错误 | `DENY` |

场景 04 的设计要点：Ed25519 验签通过只说明“信封公钥确实签了这份证明”，
真正阻止证明在构建器之间复用的是**公钥与构建器注册身份的绑定检查**。

另外测试还覆盖：rogue 构建器自报身份 + 恶意材料来源（`trusted_builder`、
`material_origins`、`materials_complete`、`output_match` 同时 FAIL）、
直接篡改签名字节（`signature` FAIL）、非法 JSON 返回 400、健康检查等。

## 测试

```bash
cargo test
```

实测结果（本机，2026-09-24）：

```
单元测试 src/crypto.rs ...... 3 passed
集成测试 tests/api.rs ........ 9 passed
合计 12 passed; 0 failed
```

`tests/api.rs` 通过 axum 内存路由（`oneshot`）发起真实 HTTP 请求，
无监听端口、无外部进程依赖。

## 目录结构

```
Cargo.toml / Cargo.lock        # 依赖清单与锁定版本
src/
  main.rs                      # provenance-server 入口
  lib.rs
  model.rs                     # Statement / Envelope / Digest 等数据结构
  crypto.rs                    # 规范化编码 + Ed25519 签名/验签（ed25519-dalek）
  policy.rs                    # 可信构建器 / 源仓库 / 材料来源策略
  verify.rs                    # 逐条策略判定引擎
  api.rs                       # Axum 路由
  fixture/
    mod.rs                     # 确定性身份派生、夹具加载/持久化
    data.rs                    # 内联材料内容、源提交、构建器 ID 等常量
    examples.rs                # 四个验收场景的请求构造
  bin/gen_fixtures.rs          # 夹具生成工具
tests/api.rs                   # 端到端测试
fixtures/                      # 已生成的本地夹具（策略、私钥、示例请求）
run_demo.sh                    # 一键演示脚本
```

## 安全模型与已知边界（演示用途）

- **规范化签名**：对固定字段顺序的紧凑 JSON 签名；它是本项目自定义的最小方案，
  生产系统建议改用 in-toto attestation / DSSE 的成熟编码，避免直接对原始 JSON 签名。
- **私钥管理**：构建器私钥以明文 hex 存于 `fixtures/builders/*.secret.hex`，
  仅用于本地演示和 `/v1/fixture-sign` 辅助端点；生产环境应由 KMS/HSM 持有，
  且不应有任何签名辅助端点对外暴露。
- **密钥派生**：夹具密钥由固定标签 SHA-256 确定性派生（无随机源，便于复现），
  不具备真实密钥的保密性。
- **无吊销/时间戳/证书链**：策略只做静态公钥绑定，不检查证明新鲜度、密钥吊销，
  也没有 TUF 之类的更新根；材料来源仅做 URI 前缀匹配（前缀强制以 `/` 结尾，
  避免越过目录边界）。
- 摘要算法只接受 `sha256`。

## 未完成项 / 可扩展方向

- 未实现 in-toto v1 / DSSE 信封兼容（当前为教学用简化结构）；
- 未做构建器授权范围（哪些 builder 允许构建哪些 repo）的细粒度矩阵；
- 无速率限制、鉴权中间件与结构化访问日志（纯后端接口，演示不涉及）；
- 策略文件修改后需重启生效（未做热加载）。
