# 镜像离线安全策略（mirror-admission）

纯后端的**本地镜像准入判定**服务。在**完全离线**环境中，基于镜像配置（OCI image
config）、SBOM、以及由**本地测试验签器**真实签名的结果，对「镜像能否进入隔离镜像
仓库 / 能否在本地使用」做准入判定。不连接任何 Kubernetes 集群或镜像仓库。

- **语言 / 框架**：Go 1.22 + [OPA Rego](https://www.openpolicyagent.org/)（嵌入式
  评估，非外部 OPA 进程）+ [chi](https://github.com/go-chi/chi) v5 路由。
- **密码学**：真实 Ed25519（RFC 8032）签名/验签，SHA-256 内容摘要，规范化 JSON
  （canonical JSON）做字节绑定。无任何桩实现、无 mock 判定。
- **依赖**：已 `vendor/` 锁定，`go.sum` 校验，可用 `GOPROXY=off` 完全离线构建。

---

## 1. 安全语义（先读这一节）

### 1.1 四条冻结规则（policy_version `1.4.0`）

| 规则 ID | 检查内容 | deny 条件 | unknown 条件（缺证据，不放行） |
|---|---|---|---|
| `no_root_user` | 镜像是否以 root 运行 | `User` 显式为 `""`/`root`/`0`/`0:0` 等 | **`User` 字段缺失**；或非数字用户名（如 `nobody`）无法在不接入集群的情况下证明非 root |
| `no_privileged` | 特权要求 | `Privileged==true`，或申请 `CAP_SYS_ADMIN`（大小写不敏感） | **`Privileged` 缺失或不是显式布尔 `false`** |
| `base_image_allowed` | 基础镜像允许列表 | SBOM 中 `base_image_digest` 不在冻结允许列表 | SBOM **没有** `base_image_digest` |
| `attestation_verified` | 本地测试验签结果 | 验签失败 / 摘要不匹配（标签漂移）/ 测试结果为 `fail` | **没有提供**验签报告 |

**核心原则：缺证据 → `UNKNOWN` 或 `DENY`，绝不能因为字段缺失而默认通过。**
字段「不存在」和字段「显式为空 / false」在策略中被严格区分（缺失用 `null`
哨兵处理，而不是 `object.get(..., false)` 这种会把缺失当默认值的写法）。

### 1.2 聚合判定

```
任一规则 deny    -> DENY
否则任一 unknown -> UNKNOWN
否则             -> ALLOW
```

每条规则都会在报告里返回**逐条理由（reason）**，并携带冻结的策略版本与策略文件
SHA-256。

### 1.3 豁免（exemption）的三重绑定

豁免由**独立的豁免授权密钥**签名（与测试器密钥分离，启动时强制两钥不同）。一个
豁免只能：

1. 绑定**唯一一个镜像 digest**（`sha256:<64hex>`，不接受通配符 / 标签）；
2. 绑定**唯一一条具体规则 ID**（不能豁免「全部规则」）；
3. 绑定**绝对到期时间 `not_after`**。

豁免生效条件（Rego 内强校验）：

```
ex.image_digest == 评估镜像 digest  AND  ex.rule ∈ 冻结规则集  AND  ex.not_after > now
```

- **边界时间**：`not_after == now` 即视为**已过期**（严格大于），原 deny 保持有效。
- 豁免**只能把 deny 降级为 exempt，永远不能消除 unknown**（缺证据不可豁免）。
- 签了名但越界（digest 不符 / 规则名错 / 过期）的豁免会在报告 `unused_exemptions`
  中逐条说明原因。
- 任一豁免**签名验不过 → 整个请求 400 拒绝**，不存在「忽略坏豁免继续评估」。

### 1.4 策略冻结与报告不可变

- `policy/admission.rego` 的 SHA-256 记录在 `policy/policy_freeze.lock.json`。
  服务器启动时逐字节校验，哈希或版本不符 → **拒绝启动**。改策略后必须显式执行
  `make policy-lock`（一次有意识的发布动作）。
- 报告是**只追加（append-only）**的 JSONL。重新评估同一镜像会生成**新报告 ID**，
  旧报告永不被覆盖；存储层对重复 ID 直接报错。

### 1.5 标签漂移（label / re-tag drift）为何失效

验签报告的 payload 同时绑定 **image config 规范化摘要** 与 **SBOM 规范化摘要**。
攻击者即使保留旧的合法签名报告，只要镜像 config 有任何字节变化（加 label、换 tag
对应的新 config、加特权位等），canonical digest 就改变，摘要比对失败 →
`attestation_verified` 判 **deny**。标签不是身份，digest 才是。

---

## 2. 目录结构

```
.
├── cmd/
│   ├── server/        # 准入 HTTP 服务（chi）
│   ├── verifier/      # 本地测试器/豁免授权离线工具（keygen/attest/exempt/keyid）
│   ├── genexamples/   # 生成真实签名的整套示例
│   └── policylock/    # 重新冻结策略哈希
├── internal/
│   ├── api/           # chi 路由与 JSON handler（含 HTTP 测试）
│   ├── service/       # 编排：验签 -> OPA 评估 -> 落报告（含端到端攻击测试）
│   ├── policy/        # Rego 编译/冻结校验/评估（含冻结防篡改测试）
│   ├── verifier/      # Ed25519 信封验签与内容绑定（含验签测试）
│   ├── crypto/
│   │   ├── sig/       # canonical JSON + Ed25519 + PEM（含密码学测试）
│   │   └── digest/    # 规范化内容摘要
│   ├── store/         # append-only JSONL + 内存存储（含持久化测试）
│   ├── model/         # 线上协议类型
│   └── testkit/       # 测试用真实密钥生成与签名（非桩）
├── policy/admission.rego            # 冻结的 OPA 策略
├── policy/policy_freeze.lock.json   # 冻结哈希锁
├── config/allowlist.json            # 默认基础镜像允许列表
├── examples/generated/              # 真实签名的示例输入（可重新生成）
├── scripts/acceptance.sh           # 一键离线验收
├── vendor/                          # 锁定依赖
└── Makefile
```

---

## 3. HTTP API（纯 JSON）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 健康检查 |
| GET | `/v1/meta` | 返回冻结策略版本、策略哈希、允许列表版本与规则列表 |
| POST | `/v1/admission/evaluate` | 提交 `{image_config, sbom, attestation, exemptions, allowlist_version}`，返回不可变报告（201，带 `Location`） |
| GET | `/v1/reports` | 列出全部历史报告（只追加） |
| GET | `/v1/reports/{id}` | 取指定报告；不存在返回 404 |

请求体字段未知 → 400（`DisallowUnknownFields`，防止塞「silent_allow」之类隐藏
开关）。请求体上限 16 MiB。

### 报告示例（节选）

```json
{
  "id": "rpt_…", "decision": "DENY",
  "policy_version": "1.4.0",
  "policy_sha256": "2c6bc6b8…",
  "allowlist_version": "allowlist-2026.09.24",
  "image_digest": "sha256:…", "sbom_digest": "sha256:…",
  "findings": [
    {"rule":"no_root_user","status":"deny","reason":"image config declares root user (User=\"root\")","exemptions":[]},
    {"rule":"no_privileged","status":"allow","reason":"Privileged explicitly false and no dangerous capabilities added","exemptions":[]},
    {"rule":"base_image_allowed","status":"allow","reason":"base image sha256:aaa… is on the allowlist","exemptions":[]},
    {"rule":"attestation_verified","status":"allow","reason":"verifier result signed by ed25519:…; all bound digests match","exemptions":[]}
  ],
  "unused_exemptions": []
}
```

---

## 4. 本地启动（从零）

前置：Go 1.22+。依赖已 vendor，无需联网。

```bash
# 1) 生成真实签名示例（含两套真实 Ed25519 密钥、12 个可直接 POST 的请求）
make examples

# 2) 直接用示例信任根启动（监听 :8080，报告落 data/reports.jsonl）
make run
# 或手动指定（等价）：
# MIRRORAD_VERIFIER_PUB=examples/generated/keys/tester_public.pem \
# MIRRORAD_EXEMPT_PUB=examples/generated/keys/exempt_authority_public.pem \
# MIRRORAD_ALLOWLIST=examples/generated/allowlist.json \
# MIRRORAD_REPORTS=data/reports.jsonl \
# ./bin/mirror-admission-server -listen :8080
```

启动日志会打印：冻结策略版本+哈希、允许列表版本、两个**不同**信任根的 key id。

### 用 verifier 工具自己产出签名证据

```bash
# 为生产/本地使用生成两套独立密钥（默认输出到 keys/）
./bin/verifier keygen -outdir keys

# 本地测试器：对真实镜像 config / SBOM 字节计算摘要并签名
./bin/verifier attest \
  -image examples/generated/configs/01-allow-clean.json \
  -sbom  examples/generated/sboms/01-allow-clean.json \
  -result pass -key keys/tester_private.pem \
  -out /tmp/att.json

# 豁免授权：为【单个 digest + 单条规则 + 到期时间】签名
# image digest = sha256  over canonical JSON of the OCI image config
D=$(python3 - path/to/config.json <<'PY'
import sys, json, hashlib
b = open(sys.argv[1], 'rb').read()
c = json.dumps(json.loads(b), sort_keys=True, separators=(',', ':')).encode()
print('sha256:' + hashlib.sha256(c).hexdigest())
PY
)
./bin/verifier exempt \
  -image-digest "$D" -rule no_root_user \
  -not-after 2026-10-02T00:00:00Z -id ex-$(date +%s) \
  -reason "break-glass migration" \
  -key keys/exempt_authority_private.pem -out /tmp/ex.json
```

服务器只加载**公钥**；私钥永远不进准入服务目录。

---

## 5. 验收命令

```bash
# 一键：离线构建 + 全量 go test（-race 可选）+ 起服务 + 发 12 个真实签名场景
#       + 边界时间 + 重评估不可变 + 防篡改策略 + 越权签名豁免
make acceptance
# 或 ./scripts/acceptance.sh
```

期望结尾：

```
================ ACCEPTANCE SUMMARY ================
passed: 18   failed: 0
ACCEPTANCE PASSED — no cluster was contacted
```

单独的常用命令：

```bash
make test           # 全量单元/集成测试（离线 vendor）
make test-race      # 带竞态检测
make vet            # go vet
make build          # 构建四个二进制到 bin/
go test -race ./... # 直接用 go
```

### 手工快速验证

```bash
curl -s localhost:8080/v1/meta | python3 -m json.tool

# 干净镜像 -> ALLOW
curl -s -X POST localhost:8080/v1/admission/evaluate \
  -H 'Content-Type: application/json' \
  --data-binary @examples/generated/requests/01-allow-clean.request.json \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["decision"])'

# 缺 User 字段（恶意缺字段）-> UNKNOWN，而不是放行
curl -s -X POST localhost:8080/v1/admission/evaluate \
  -H 'Content-Type: application/json' \
  --data-binary @examples/generated/requests/07-unknown-missing-user.request.json \
  | python3 -m json.tool
```

### 12 个示例场景的期望结果

| 请求文件 | 决策 | 验证点 |
|---|---|---|
| 01-allow-clean | ALLOW | 非 root + 显式非特权 + 允许基础镜像 + 验签通过 |
| 02-deny-root | DENY | `User=root` |
| 03-deny-uid0 | DENY | `User=0:0` |
| 04-deny-privileged | DENY | `Privileged=true` |
| 05-deny-sysadmin-cap | DENY | 申请 `CAP_SYS_ADMIN` |
| 06-deny-base-not-allowed | DENY | 基础 digest 不在允许列表 |
| 07-unknown-missing-user | **UNKNOWN** | User 字段缺失 → 不当 root 判、更不放行 |
| 08-unknown-no-attestation | **UNKNOWN** | 无验签报告 |
| 09-deny-verifier-fail | DENY | 验签合法但测试结果为 fail |
| 10-exempt-root-valid | ALLOW | root deny 被**同 digest/同规则/未过期**豁免降为 exempt |
| 11-exempt-boundary-expired | ALLOW（当前时间）/ **DENY**（恰在到期时刻） | 边界时间 |
| 12-exempt-wrong-digest | DENY | 豁免签名合法但绑定**别的 digest**（越界） |

复现边界时间（服务器固定时钟，仅测试用）：

```bash
MIRRORAD_NOW=2026-10-01T00:00:00Z ... ./bin/mirror-admission-server   # 恰到期 -> DENY
MIRRORAD_NOW=2026-09-30T23:59:59Z ... ./bin/mirror-admission-server   # 差 1 秒 -> ALLOW
```

---

## 6. 自动化测试覆盖

| 包 | 覆盖内容 |
|---|---|
| `internal/crypto/sig` | canonical JSON 稳定性/拒绝尾随数据、Ed25519 往返、篡改载荷/翻转签名/异钥验签、key id 稳定性与唯一性 |
| `internal/policy` | 冻结锁加载、版本自检、**篡改一个字节即拒绝启动** |
| `internal/verifier` | 验签往返、错误 digest 绑定、豁免结构校验、伪造 `signer_key_id`、垃圾信封 |
| `internal/service` | 四条规则正/反例、**缺字段→unknown**、**标签漂移**、测试器 fail、豁免生效/越 digest/边界时间/不豁免 unknown、**用测试器密钥伪造豁免被拒**、篡改签名、允许列表版本不匹配、坏 JSON、**重评估不覆盖旧报告**、逐条理由完整性 |
| `internal/store` | JSONL 重启后历史保留、追加顺序、重复 ID 拒绝覆盖、404 |
| `internal/api` | health/meta、201+Location、报告取回、未知字段 400、列表/404、HTTP 层标签漂移 |

所有签名与摘要在测试中都是**真实执行**（测试用密钥每次由 `crypto/rand` 真实
生成），不存在伪造的密码学结果。

---

## 7. 设计说明与取舍

- **OPA 嵌入式评估**：以库方式编译冻结 Rego，避免再拉起一个 OPA 进程；策略编译
  在启动期完成（`PrepareForEval`），请求路径只做求值。
- **Go 负责密码学与结构校验，Rego 负责策略判定**：签名、密钥身份、digest 绑定在
  Go 层完成（错误不可降级）；Rego 只接收「已验/未验」结论与原始证据，专注规则与
  豁免范围/到期逻辑。
- **规范化 JSON 摘要**：签名绑定在「规范化 JSON」上，因此传输时的缩进/键序差异不
  会造成假阴性，但任何**内容**变化（含标签漂移）都会改变摘要。HTML 转义关闭、
  数字以 `json.Number` 保留，保证签名方与验签方字节一致。
- **两种「证据缺失」的区分**：根本没提交验签报告 → unknown；提交了但验签失败 /
  结果 fail → deny（这是「存在但不可信/失败」的正向证据）。
- **职责分离**：测试器密钥只能签发测试结果，豁免授权密钥只能签发豁免；启动时
  拒绝两把相同的信任根。

---

## 8. 重新冻结 / 更换允许列表

```bash
# 评审并修改 policy/admission.rego 后，显式重新冻结
$EDITOR policy/admission.rego
make policy-lock      # 重新生成 policy_freeze.lock.json；记得同步引擎 version 常量

# 更换允许列表：编辑 config/allowlist.json（version 必须变化，客户端按版本钉住）
$EDITOR config/allowlist.json
```

允许列表只接受 `sha256:<64 位小写 hex>` 条目，重复条目启动报错；请求必须显式声明
`allowlist_version` 且与服务端一致，否则 400（防止用过时列表评估）。
