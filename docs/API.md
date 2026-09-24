# HTTP API

所有请求/响应均为 JSON，不依赖任何集群或仓库连接。

## 线协议约定

`AdmissionRequest` 的证据字段携带的是 **base64(JSON 原文)**（不是嵌套对象）：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `image` | base64(JSON) | 是 | 镜像运行配置；空/非法 => `422` fail-closed |
| `sbom` | base64(JSON) | 否 | 缺失 => `IMG-SBOM = UNKNOWN` |
| `verification` | base64(Envelope) | 否 | 本地验签器结果；缺失 => `IMG-SIGNED = UNKNOWN` |
| `exemptions` | base64(Envelope[]) | 否 | 豁免书信封数组；逐封验签，损坏件记 DENY |

> 设计要点：证据用**不透明 base64 字节**而非解析后的对象传递，服务端自行重新
> canonical 化并验签，避免调用方“顺手”改写载荷；摘要/签名都对原始字节计算。

## 端点

### GET /healthz
```json
{"status":"ok"}
```

### GET /v1/policy
返回冻结的策略版本、哈希与允许列表。
```json
{
  "version": "1.0.0-frozen",
  "hash": "sha256:1c7a...",
  "allowlist": [{"repository":"registry.local/base/distroless","digest":"sha256:71b2..."}]
}
```

### POST /v1/admission/evaluate
请求体（注意 base64）：
```json
{
  "image": "eyJyZXBvc2l0b3J5Ijoi...",
  "sbom": "eyJib21Gb3JtYXQiOi...",
  "verification": "eyJwYXlsb2FkIjoi...",
  "exemptions": "W3sicGF5bG9hZCI6Ii4uIn1d"
}
```
- HTTP 始终 `200`（准入类 webhook 惯例），准入结论以 `decision` 为准；
  仅当请求本身无法评估（缺 image、非法 JSON）时返回 `400/422`。

响应 `Report`：
```json
{
  "id": "rep_48d08d1a7a62cf9389ffe961",
  "createdAt": "2026-09-23T17:40:01.123456789Z",
  "policyVersion": "1.0.0-frozen",
  "policyHash": "sha256:1c7a...",
  "repository": "registry.local/app/payments",
  "tag": "v2.1.0",
  "imageDigest": "sha256:21e64d9...",
  "requestDigest": "sha256:9af0...",
  "decision": "ALLOW",
  "findings": [
    {"ruleId":"IMG-RUN-ROOT","title":"禁止以 root 运行","status":"ALLOW","reason":"user=\"appuser:10001\" 非 root"},
    {"ruleId":"IMG-RUN-ROOT","status":"EXEMPT","reason":"...（已被豁免书 exm-x 豁免...）","exemptionId":"exm-x"}
  ],
  "exemptionsSeen": [
    {"id":"exm-x","digest":"sha256:...","ruleId":"IMG-RUN-ROOT",
     "expiresAt":"2026-10-01T00:00:00Z","expiresNs":1780272000000000000,"validSignature":true}
  ]
}
```

`findings[].status`：`ALLOW | DENY | UNKNOWN | EXEMPT`。
`decision`：`DENY > UNKNOWN > ALLOW`。

### GET /v1/reports?limit=N
按创建时间倒序列出报告摘要（只读，不修改任何报告）。

### GET /v1/reports/{id}
读取单份历史报告。重评估产生新 `id`，旧报告永不改变。不存在/路径穿越 => `404`。

## 错误体
```json
{"error":{"code":"EVALUATION_FAILED","message":"请求缺少 image（无镜像配置无法评估，fail-closed）"}}
```
| HTTP | code | 触发 |
|---|---|---|
| 400 | `BAD_REQUEST` | 请求体非法 JSON / limit 非法 |
| 422 | `EVALUATION_FAILED` | 缺 image、镜像或 SBOM 非法 JSON、豁免数组非法 |
| 404 | `NOT_FOUND` | 路径或报告不存在 |
| 500 | `STORE_ERROR` | 报告持久化失败（含“同 ID 拒绝覆盖”） |

## 规则 ID

| ID | 检查 | 缺证据时 |
|---|---|---|
| `IMG-RUN-ROOT` | 运行用户是否 root（`root`/`0`/`0:gid`/空串） | 缺 `user` => UNKNOWN |
| `IMG-PRIVILEGED` | `privileged=true` 或 `capAdd` 含 `ALL` | 缺 `privileged` => UNKNOWN |
| `IMG-BASE-ALLOWLIST` | 基础镜像 repo+digest 命中允许列表 | 缺 `baseImage`/digest => UNKNOWN |
| `IMG-SBOM` | SBOM 存在且被验签结果背书 | 缺 SBOM => UNKNOWN |
| `IMG-SIGNED` | 验签器确认镜像签名有效且签名者受信 | 缺验签结果 => UNKNOWN |
| `IMG-DIGEST-BIND` | 验签结果/SBOM 绑定摘要与实际一致 | 不一致 => DENY |
| `IMG-EXEMPTION-INVALID` | 豁免伪造/越权/挪用/过期 | 任一成立 => DENY |
