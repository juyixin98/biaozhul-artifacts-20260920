# build-attestation — 本地构建证明验签服务（纯后端）

一个从零实现的小型**构建证明（build attestation）签发/验证**演示服务：builder 对
产物 digest、源码提交、构建参数及自身身份做 Ed25519 签名；验证方用本地信任策略
（builder 白名单 + 源码来源白名单 + 密钥轮换/撤销时间窗）判定是否接受。

> **明确声明：这不是完整的 Sigstore 实现。** 本格式没有透明日志（Rekor）、没有
> 证书链 / Fulcio、没有 OIDC 联邦、没有时间戳机构（TSA），信任锚是一个静态本地
> 策略文件。它只是一个带域分隔、规范 JSON 与策略校验的自定义证明格式，用于展示
> “签名有效 ≠ 策略放行”这一完整验签链路。签名格式为自定义的
> `application/vnd.example.build-attestation.v1`。

## 密码学与协议（真实执行，非模拟）

- **签名**：Go `crypto/ed25519`（Ed25519，标准库，无第三方密码学依赖）。
- **签名消息**：`"BUILD_ATTESTATION/v1\n" + canonicalJSON(statement)`，域分隔前缀
  防止同一密钥的其他协议签名被当作证明接受（跨用途签名拒绝），也防止本格式签名被
  拿去冒充别的协议。
- **一次签名绑定全部声明**：`builderId`、`keyId`、`source.repository`、
  `source.commit`、每个产物 `subjects[].{alg,value}`（sha256 十六进制）、
  `params`、`issuedAt`（RFC3339 UTC）、`nonce`。改动其中任一字节即验签失败。
- **封装**：显式 JSON envelope
  `{"payloadType":..., "payload": <base64url(canonicalJSON)>, "signature": <base64url(ed25519 raw)>}`。

### 规范 JSON（自定义最小规范，不是 RFC 8785/JCS）

见 `internal/canonical`，规则固定且可测：

1. 对象键按 UTF-8 字节序升序排列，**重复键直接拒绝**（嵌套对象同样检查）；
2. **仅允许整数**且必须是规范字面量：禁止 `1.0`、`1e3`、`01`、`-0`、分数/指数；
3. 无任何无关空白；字符串只转义 `"`、`\` 与控制字符，`/` 与非 ASCII 不转义；
4. 拒绝 BOM、非法 UTF-8、尾随数据；
5. 线上 payload 必须**逐字节**等于“解析→重编码”的结果，否则
   `NON_CANONICAL_PAYLOAD`——防止签名只覆盖等价文档的另一种字节形态。

### 信任策略（`testdata/policy.json`）

- `allowedSourcePrefixes`：源码仓库白名单（前缀/命名空间匹配）；
- 每个 `builder` 持有若干密钥，每把密钥有 `notBefore` / `notAfter` **轮换窗口**和
  可选 `revokedAt` **撤销时刻**；时间窗按证明的 `issuedAt` 判定（撤销前签发仍有效，
  撤销后签发拒绝）；
- keyId 必须注册在 envelope 所声明的 builder 名下（A builder 的有效密钥不能替 B
  builder 背书）。

### 验证顺序（`internal/attestation/verify.go`）

信封解析（拒重复/未知字段）→ payloadType 用途锁定 → base64url 与长度检查 →
**规范字节逐字节比对** → 声明结构校验（未知字段拒绝）→ builder/key 策略匹配 →
密钥生效/过期/撤销窗口 → 源码来源白名单 → Ed25519 验签 → `issuedAt` 新鲜窗口
（默认 ±5 分钟）→ nonce 去重（重放拒绝，内存缓存、TTL 过期、容量满则 fail-closed）。
重放缓存在最后一步写入，畸形/无效证明不会“毒化”缓存。

## 目录结构

```
cmd/attestation-server/   HTTP 验签服务
cmd/sign-attestation/     用测试密钥对本地产物实时签发（生成新鲜 nonce/时间戳）
cmd/gen-examples/         生成 testdata/policy.json 与 examples/ 下的全部样本
internal/canonical/       规范 JSON 编解码
internal/attestation/     证明格式、签名、策略、验签
internal/httpapi/         HTTP handler
internal/testkeys/        确定性 Ed25519 测试密钥（仅测试用，由公开标签派生，非秘密）
examples/                 有效证明与各类攻击/违规样本
scripts/acceptance.sh     一键端到端验收
```

## 依赖（已锁定）

仅依赖 Go **标准库**（`crypto/ed25519`、`net/http`、`encoding/json` 等）。
`go.mod` 锁定 `go 1.22`，无任何第三方 require，因此没有额外供应链依赖；
`go.sum` 因此为空/不存在。构建可用 `GOTOOLCHAIN=local GOFLAGS=-mod=readonly` 复现。

## 本地启动

前置：Go 1.22+、curl（验收脚本另需 python3）。

```bash
# 1) 生成测试策略与样本（首次运行；产物已在仓库中，可重复生成）
go run ./cmd/gen-examples

# 2) 启动验签服务（默认 127.0.0.1:8080，±5 分钟新鲜窗口）
go run ./cmd/attestation-server -addr 127.0.0.1:8080 -policy testdata/policy.json
```

健康检查：`curl -s http://127.0.0.1:8080/healthz`

## 手工验收命令

```bash
# 用真实时钟签发一份新鲜证明（对 examples/artifact.bin 求 sha256 后签名）
go run ./cmd/sign-attestation -out /tmp/fresh.json
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' --data-binary @/tmp/fresh.json

# 再发一次同一文件 → 409 REPLAYED_NONCE（重放拒绝）
curl -s -i -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' --data-binary @/tmp/fresh.json
```

固定时间戳的样本（`examples/`，issuedAt=2026-09-15T12:00:00Z）需要把服务时钟钉到
签发时刻才能通过新鲜窗口，仅用于可复现演示：

```bash
go run ./cmd/attestation-server -addr 127.0.0.1:8080 -now 2026-09-15T12:00:00Z
# 另一个终端：
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' --data-binary @examples/valid-envelope.json
#   -> {"accepted":true,...}
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' --data-binary @examples/tampered-digest.json
#   -> {"accepted":false,"code":"BAD_SIGNATURE",...}
```

### 样本与预期结论（钉时钟方式运行）

| 文件 | 结果码 | 说明 |
|---|---|---|
| `valid-envelope.json` | accepted | 完整有效证明 |
| `tampered-digest.json` | `BAD_SIGNATURE` | 改产物 digest 后保留旧签名 |
| `old-key.json` | `KEY_EXPIRED` | 轮换窗口外的旧密钥签发 |
| `extra-field.json` | `INVALID_STATEMENT` | 签名有效但含未声明额外字段 |
| `cross-purpose.json` | `BAD_SIGNATURE` | 用其他协议域前缀签名 |
| `untrusted-source.json` | `UNTRUSTED_SOURCE` | 签名有效但源码来源不在白名单 |
| `noncanonical-wire.json` | `NON_CANONICAL_PAYLOAD` | payload 非规范字节（插入空白） |
| `duplicate-key.json` | `DUPLICATE_JSON_KEY` | JSON 重复键 |

测试还覆盖：篡改 commit/params、未知 builder、跨 builder 密钥、密钥**撤销**窗口
（撤销时刻前后行为不同）、`notBefore` 未生效、新鲜窗口边界、payloadType 不匹配、
非 UTC 时间戳、畸形输入等。

## 自动化测试 / 一键验收

```bash
go test ./... -v          # 单元 + HTTP 集成测试（全部为真实 Ed25519 运算）
gofmt -l . && go vet ./...
./scripts/acceptance.sh   # 构建+测试+起服务+逐样本 curl 断言+真实时钟签发/重放
```

`scripts/acceptance.sh` 会对每个样本断言 HTTP 状态与机器可读 `code`，并额外完成：
同一份有效证明提交两次（第二次必须 `409 REPLAYED_NONCE`）、用真实时钟签发的新鲜
证明必须 `200 accepted=true` 且重放被拒。

## 测试密钥警告

`internal/testkeys` 的密钥由公开标签经 SHA-256 确定性派生（种子写在源码里），
**任何人都能重现私钥，严禁用于真实信任锚**。真实部署应改为从 KMS/HSM 注入私钥、
用带外流程分发公钥策略。

## HTTP 接口

- `GET /healthz` → `200 {"status":"ok"}`
- `POST /verify`，body 为 envelope JSON（≤1 MiB）：
  - `200` `{"accepted":true,"result":{...}}`
  - `400` 畸形/非规范/结构非法（`MALFORMED`、`NON_CANONICAL_PAYLOAD`、
    `DUPLICATE_JSON_KEY`、`INVALID_STATEMENT`）
  - `403` 签名或策略拒绝（`BAD_SIGNATURE`、`CROSS_PURPOSE_SIGNATURE`、
    `UNKNOWN_BUILDER`、`UNKNOWN_KEY`、
    `KEY_NOT_YET_VALID`、`KEY_EXPIRED`、`KEY_REVOKED`、`UNTRUSTED_SOURCE`、
    `ISSUED_AT_OUTSIDE_WINDOW`）
  - `409` `REPLAYED_NONCE`
