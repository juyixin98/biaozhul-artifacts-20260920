# 示例输入

本目录由 `go run ./cmd/genexample -out examples` 生成。**所有签名都是真实 Ed25519 计算**，
重新执行会用相同密钥（若 `keys/` 存在）或新密钥重新签名全部文件。

## 文件

| 文件 | 含义 |
|---|---|
| `artifacts/{a,b,c}.bin` | 三个本地产物，内容不同，digest 不同 |
| `evidence-a-v1.json` | 产物 A 的测试证据 v1：unit/integration 通过，security_scan=false |
| `evidence-a-v2.json` | 产物 A 的证据 v2：security_scan 也通过（不可变新版本，非覆盖） |
| `evidence-b-v1.json`、`evidence-c-v1.json` | B/C 的证据 v1（全部必需测试通过） |
| `policy-v1.json` | 策略 v1：仅要求 unit+integration，无需审批，keep_last_n=3 |
| `policy-v2.json` | 策略 v2：增加 security_scan 与人工审批要求，keep_last_n=2 |
| `policy-v3.json` | 策略 v3：准入同 v2，保留规则更宽松 keep_last_n=4（回退演示用） |
| `approval-a-staging-gen0.json` | 审批人对「A→staging、policy v2、evidence ev-a v2、gen 0」元组的真实签名 |
| `keys/ci.{pub,key}` | CI 测试签名密钥 |
| `keys/policy-authority.{pub,key}` | 策略签发密钥 |
| `keys/approver.{pub,key}` | 人工审批密钥（验收脚本用它签其他代次的审批） |

## 现场签一个其他代次的审批

```bash
go run ./cmd/signapproval \
  -key examples/keys/approver.key \
  -id apr-b-staging-gen1 \
  -policy promotion-policy -policy-version 2 \
  -env staging \
  -digest "$(jq -r .artifact_digest examples/evidence-b-v1.json)" \
  -evidence ev-b -evidence-version 1 -expected-gen 1
```

输出是可直接 `POST /v1/approvals` 的 JSON，签名负载与服务端验签使用同一份
canonical JSON（见 `internal/approval` 与 `internal/crypto/canon`）。
