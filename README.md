# 构建证明验签服务（buildattest）

本地构建证明（build attestation）验证服务。纯 Go 标准库实现（`crypto/ed25519` +
`net/http`），无第三方依赖。证明采用规范 JSON 封装，ed25519 签名绑定
**产物 digest、源码 commit、构建参数与 builder 身份**；信任策略限制可信
builder 与源码仓库，**签名有效但策略不符仍拒绝**。

> ⚠️ **范围声明**：这是一个自定义的、教学/内部用途的证明格式与验证服务，
> **不冒充、也不兼容完整的 Sigstore 体系**（无 Rekor 透明日志、无 Fulcio
> 证书链、无 OIDC 身份签发、无 in-toto/SLSA 谓词语义）。生产环境请评估
> Sigstore / in-toto 等成熟方案。

## 格式

### 封装（envelope）

```json
{
  "attestation": {
    "attestationId": "att-<随机>",
    "purpose": "build-attestation/v1",
    "artifactDigest": "sha256:<64 位小写 hex>",
    "sourceRepo": "https://github.com/example/project",
    "sourceCommit": "<40 位小写 hex>",
    "builder": "builder-alice",
    "buildParams": {"goarch": "amd64", "goos": "linux"},
    "timestamp": "2026-09-24T12:00:00Z"
  },
  "signature": {
    "keyId": "key-2026a",
    "algorithm": "ed25519",
    "sig": "<base64>"
  }
}
```

被签名的字节为：

```
"ATTESTATION-V1\x00" || CanonicalJSON(attestation)
```

- **域分隔前缀** + **purpose 字段**双重绑定用途，拒绝跨用途签名
  （例如把登录挑战的签名伪装成构建证明）。
- **规范 JSON**：对象键字典序、无空白、字符串最小转义；同一逻辑值唯一
  对应一份字节，可直接作为签名输入。

### 严格解析（拒绝项）

- 重复 JSON 键；
- 非规范数值：仅接受无前导零的整数（`0`、`-3`、`42`），拒绝
  `1.0`、`1e3`、`-0`、`01` 等；
- 封装 / 证明 / 签名对象中的**未知（额外）字段**与缺失字段；
- 非 UTC（不以 `Z` 结尾）或亚秒精度的时间戳；
- 顶层值之后的尾随数据。

### 信任策略与密钥轮换

策略文件（见 `examples/policy.json`）：

```json
{
  "trustedBuilders": ["builder-alice"],
  "allowedSourceRepos": ["https://github.com/example/project"],
  "maxAttestationAgeSeconds": 600,
  "keys": [
    {"keyId": "key-2026a", "publicKey": "<base64>",
     "validFrom": "2026-01-01T00:00:00Z", "validUntil": "2027-01-01T00:00:00Z"}
  ]
}
```

- 每把密钥保留 `validFrom`（生效）与 `validUntil`（撤销）时间；轮换时
  新旧密钥并存，各带时间窗。验证时要求**当前时刻**与**证明时间戳**都落
  在密钥窗口内——旧密钥撤销后，即使签名本身有效也被拒绝。
- `builder` 必须在 `trustedBuilders`、`sourceRepo` 必须在
  `allowedSourceRepos` 中，否则拒绝（策略优先于签名有效性）。
- `attestationId` 一次性使用：服务端记录已接受的 ID，重复提交即重放拒绝；
  证明年龄超过 `maxAttestationAgeSeconds` 也被拒绝。

### 验证流水线顺序

严格解析 → 结构/格式 → purpose → 密钥窗口 → ed25519 验签 →
builder/仓库策略 → 时效 → 重放登记。

## 目录

```
cmd/server/       验签 HTTP 服务（POST /v1/verify, GET /healthz）
cmd/attestgen/    测试工具：keygen 生成测试密钥、sign 签发证明
internal/attest/  核心库：严格 JSON、规范化、策略、验签、HTTP
examples/         示例策略、测试密钥、示例证明
```

## 本地启动

```sh
go build ./...
go run ./cmd/server -addr 127.0.0.1:8080 -policy examples/policy.json
```

## 验收命令

```sh
# 1. 自动化测试（覆盖：改 digest、旧密钥、额外字段、重放、重复键、
#    非规范数值、跨用途、不可信 builder/仓库、轮换等）
go test ./...

# 2. 签发一份新证明（示例 attestation-valid.json 有时效，过期后需重新签发）
go run ./cmd/attestgen sign -key examples/testkey.json \
  -digest sha256:$(printf 'a%.0s' {1..64}) \
  -repo https://github.com/example/project \
  -commit $(printf 'b%.0s' {1..40}) \
  -builder builder-alice -param goos=linux -param goarch=amd64 \
  -out /tmp/att.json

# 3. 验证：应返回 200 {"accepted":true,...}
curl -s -w '\nHTTP %{http_code}\n' -X POST http://127.0.0.1:8080/v1/verify \
  --data-binary @/tmp/att.json

# 4. 重放：再发一次，应返回 422 重放拒绝
curl -s -X POST http://127.0.0.1:8080/v1/verify --data-binary @/tmp/att.json

# 5. 篡改 digest：应返回 422 签名验证失败
sed 's/aaaaaa/cccccc/' /tmp/att.json > /tmp/tampered.json
curl -s -X POST http://127.0.0.1:8080/v1/verify --data-binary @/tmp/tampered.json
```

## 依赖锁定

仅使用 Go 标准库，`go.mod` 无任何 `require`，依赖天然锁定；
`go build` / `go test` 全程不访问网络。测试密钥见
`examples/testkey.json`（文件内含 `warning` 字段，**仅限本地测试**）。
