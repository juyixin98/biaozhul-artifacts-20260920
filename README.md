# mirrorsec —— 镜像离线安全策略（Image Offline Admission Policy）

在**离网/气隙环境**中判定一个容器镜像是否允许被拉取运行。纯后端：Go + OPA(Rego) + Chi，
**不连接任何真实集群、镜像仓库或外部服务**；摘要、签名、验签全部本地真实执行。

- **判定输入**：镜像运行配置（裁剪的 config）、SBOM、本地测试验签器产出的验签结果、豁免书。
- **检查规则**：root 用户、特权要求（`privileged`/`capAdd=ALL`）、基础镜像允许列表、SBOM 证据、镜像签名与证据摘要绑定、豁免书合法性。
- **核心原则（fail-closed）**：缺证据判 `UNKNOWN`/`DENY`，**绝不能因字段缺失默认通过**。
- **豁免**：必须绑定**精确镜像 digest + 具体规则 + 到期时间**，且由豁免机构私钥签名；不可跨镜像挪用、不可越权豁免、`UNKNOWN` 不可豁免。
- **策略版本冻结**：策略 Rego 与允许列表随二进制嵌入并计算哈希；每份报告回带 `policyVersion`/`policyHash`。
- **报告不可变**：评估只追加新报告（新 ID），**重评估永不覆盖旧报告**。

---

## 1. 目录结构

```
cmd/admissiond/            HTTP 准入服务（加载信任根、冻结策略、持久化报告）
cmd/mirrorctl/             离线工具：密钥生成/摘要/签名/本地验签/签发豁免/生成示例
internal/model/            领域类型与线协议
internal/cryptox/          canonical JSON、SHA-256、ed25519 签名/验签、PEM（真实密码学）
internal/policy/           policy.rego（冻结策略）+ allowlist.json（嵌入）
internal/localverify/      本地测试验签器：真实验镜像签名并签发验签结果信封
internal/evaluate/         编排：密码学核验 + OPA 评估 -> 不可变报告
internal/store/            追加式报告存储（同 ID 拒绝覆盖）
internal/api/              Chi 路由与 HTTP 处理
examples/                  真实生成的密钥(仅演示)、镜像、SBOM、签名、验签结果、豁免
scripts/eval.py            发送单个评估请求并可断言判定
scripts/acceptance.sh      一键端到端验收（标签漂移/豁免越界/缺字段/边界时间/不可变）
vendor/                    锁定的依赖（go.sum + vendoring，可离线构建）
```

## 2. 信任模型与角色

| 角色 | 密钥（示例） | 职责 |
|---|---|---|
| 镜像签名者 | `signer.key/.pub` | 对镜像配置 canonical 摘要做 ed25519 签名 |
| 本地验签器 | `verifier.key/.pub` | 核验镜像签名（真实），并对**验签结果**签名背书 |
| 豁免签发机构 | `exemption-authority.key/.pub` | 签发绑定 digest/规则/到期时间的豁免书 |

准入服务只持有这三者的**公钥**（`examples/keys/trust.json`）。任何信封验签失败都不静默放行。

> ⚠️ `examples/keys/*.key` 是 `make examples` 在本机真实生成的**演示私钥**，仅用于本地验收，
> 请勿在生产使用；生产环境应自行生成并离线保管私钥。

## 3. 判定如何做出（计算与密码学都是真实的）

1. **镜像摘要**：对镜像配置做 canonical JSON（键排序、无空白、关闭 HTML 转义）后计算 `sha256:`。
2. **验签结果信封**：用验签器公钥对载荷重新 canonical 化后 **ed25519 验签**；
   并检查其 `imageDigest` 是否等于当前镜像摘要——不一致即**标签漂移** `IMG-DIGEST-BIND DENY`。
3. **镜像签名**：信任验签器的结论（`SIGNED/UNSIGNED/BAD_SIGNATURE/UNTRUSTED_KEY`）。
4. **SBOM**：必须被验签结果背书，且实际 SBOM 摘要等于背书摘要（张冠李戴即 DENY）。
5. **豁免书**：逐封真实验签 + 解析 RFC3339 到期时间；
   - 仅 `IMG-RUN-ROOT`、`IMG-PRIVILEGED`、`IMG-BASE-ALLOWLIST` 三条规则可豁免；
   - digest 必须与当前镜像完全一致；`now <= expiresAt`（到期时刻仍有效，过 1ns 即失效）；
   - 只对 **DENY** 生效，`UNKNOWN`（缺证据）不可豁免；
   - 越权/挪用/过期/伪造均产生独立的 `IMG-EXEMPTION-INVALID DENY`。
6. **汇总**：`DENY > UNKNOWN > ALLOW`（`EXEMPT` 计为通过）。每条规则都给出**逐条理由**。

### 状态语义

- `ALLOW`：证据齐全且全部通过；`EXEMPT`：某条 DENY 被有效豁免（理由中回带豁免 ID）。
- `UNKNOWN`：**证据不足**（缺字段/缺 SBOM/缺验签结果），不是放行——编排器应阻断或转人工。
- `DENY`：有明确违规，或签名/绑定/豁免书被证伪。

## 4. 本地启动

前置：Go 1.22+（仅构建期需要；运行期无任何外联）。

```bash
make build          # 用 vendor/ 离线编译出 bin/admissiond 与 bin/mirrorctl
make examples       # 真实生成演示密钥、镜像、SBOM、签名、验签结果、豁免
make run            # 启动在 127.0.0.1:18080，报告写入 data/reports/
```

等价的手动命令：

```bash
go run -mod=vendor ./cmd/mirrorctl gen-examples -out examples
go run -mod=vendor ./cmd/admissiond \
  -addr 127.0.0.1:18080 \
  -trust examples/keys/trust.json \
  -reports data/reports
```

查看冻结策略：

```bash
curl -s http://127.0.0.1:18080/v1/policy | jq
# { "version":"1.0.0-frozen", "hash":"sha256:...", "allowlist":[...] }
```

## 5. 验收命令（推荐）

```bash
make acceptance               # 自动构建+生成+起服务+9个场景断言+不可变性检查
# 若 18080 被占用：
PORT=19090 make acceptance
```

单独跑自动化测试（含竞态检测）：

```bash
make test          # go test ./...
make race          # go test -race ./...
```

手工发一个请求（字段在线上是 base64；脚本已处理）：

```bash
python3 scripts/eval.py 18080 "合规" \
  examples/requests/good-image.json \
  --sbom examples/requests/good-sbom.json \
  --verification examples/requests/good-verification.json \
  --expect ALLOW
```

### 验收覆盖的攻击/边界场景

| 场景 | 输入手法 | 期望 |
|---|---|---|
| 标签漂移 | 篡改镜像内容但沿用同一 tag，携带**旧**验签结果 | `DENY`（IMG-DIGEST-BIND） |
| 豁免越界 | 豁免书试图豁免 `IMG-SIGNED` | `DENY`（IMG-EXEMPTION-INVALID） |
| 豁免挪用 | 豁免绑定**别的 digest** | `DENY` 且豁免不生效 |
| 豁免过期 | `expiresAt` 早于当前时间 | `DENY`；到期**时刻**仍有效 |
| 伪造豁免 | 用非豁免机构私钥签发 / 损坏信封 | `DENY`（验签失败） |
| 恶意缺字段 | 故意不带 `user`/`privileged`/`baseImage`，不带 SBOM | `UNKNOWN`（不放行） |
| 特权提升 | `privileged=true` 或 `capAdd=["ALL"]` | `DENY` |
| 不受信签名者 | 用受信列表之外的密钥签名 | `DENY` |
| 重评估 | 同镜像再次评估 | 生成**新报告 ID**，旧报告原样保留 |

边界时间另有 Go 单元测试覆盖：到期时刻 `now==expiresAt` 判 `EXEMPT`，早 1 纳秒判过期。

## 6. 用 mirrorctl 走一遍完整流程

```bash
# 角色密钥（生产请离线生成、分开保管）
./bin/mirrorctl keygen -out keys -name signer
./bin/mirrorctl keygen -out keys -name verifier
./bin/mirrorctl keygen -out keys -name exemption-authority

# 对镜像签名（真实 ed25519 + 真实摘要）
./bin/mirrorctl sign-image -in my-image.json -key keys/signer.key -out my-signature.json

# 本地测试验签器：验镜像签名，并签发“验签结果信封”
./bin/mirrorctl verify-local -image my-image.json -sbom my-sbom.json \
  -signature my-signature.json -verifier-key keys/verifier.key \
  -trusted keys/signer.pub -out my-verification.json
# 退出码 3 = 验签器给出否定结论（未签名/签名坏/签名者不受信）

# 为某个具体 digest+规则签发有期限的豁免（只允许三条规则）
D=$(./bin/mirrorctl digest -in my-image.json)
./bin/mirrorctl grant-exemption -digest "$D" -rule IMG-RUN-ROOT \
  -expires 2026-10-01T00:00:00Z -key keys/exemption-authority.key -out my-exemption.json
```

HTTP 接口与请求/响应字段见 [docs/API.md](docs/API.md)。

## 7. 修改策略（版本冻结纪律）

- 策略源 `internal/policy/policy.rego` 与允许列表 `internal/policy/allowlist.json` 在编译期嵌入。
- 任何策略改动都**必须**提升 `internal/policy/embed.go` 中的 `Version`；`policyHash` 会随之改变，
  历史报告中保留的仍是其评估当时的版本与哈希，可据此审计“这条结论由哪版策略给出”。
- 允许列表按 **repository + 精确 digest** 匹配；可变 tag 不构成允许依据。
