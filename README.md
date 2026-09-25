# 离线 X.509 证书链验证服务（Offline Certificate Chain Verifier）

一个**纯后端、纯本地**的 X.509 证书链构建与验证服务。使用 Python 与成熟的
[`cryptography`](https://cryptography.io/) 库做密码学原语，**不自创任何加密算法**；
所有测试密钥与证书都在本机临时生成，不接触任何生产账号/真实 CA。

> **核心安全边界（务必阅读）**
>
> * **完全离线**：程序不发起任何出站连接，不按 AIA 补证书，不下载 CRL，不查询 OCSP，
>   不使用系统信任库。证书、中间证书、信任根全部由调用方在请求里显式提供。
> * **信任根显式指定**：每次请求必须显式给出 `trust_anchors`，没有"默认信任"。
> * **验证时刻显式指定**：过期判断使用请求里的 `verification_time`，不偷偷用系统当前时间。
> * **用途显式指定**：按 `purpose`（`serverAuth` / `clientAuth` / `any`）检查 EKU。
> * **主机名显式指定**：按 RFC 6125 只检查 SAN（dNSName/iPAddress），默认不回退 CN。
> * **吊销状态不验证**：返回体的 `revocation.checked` 恒为 `false`。`valid: true`
>   **不代表证书未被吊销**，只代表在给定时刻、给定信任根、给定用途/主机名之下，
>   链的签名、时间、基本约束等通过了校验。

---

## 1. 目录结构

```
.
├── certverifier/                # 主包
│   ├── models.py                # Purpose / VerificationOptions / 结果与错误类型
│   ├── pemutils.py              # PEM 解析、证书摘要、去重
│   ├── hostnames.py             # RFC 6125 SAN 主机名/IP/通配符匹配
│   ├── signature_check.py      # 签名验证（verify_directly_issued_by + 原语回退）
│   ├── chain_builder.py         # 离线链构建（DFS，按签名而非名字找父证书）
│   ├── verify.py                # RFC 5280 风格路径校验主流程
│   ├── fixtures.py              # 本机生成测试密钥与各类证书链
│   ├── service.py               # 仅监听 127.0.0.1 的 HTTP JSON 服务
│   └── cli.py                   # 命令行入口
├── scripts/
│   ├── generate_fixtures.py     # 把全部场景生成到 fixtures/
│   └── verify_request.py        # 用标准库发送本地 /verify 请求
├── fixtures/                    # 生成产物（root/intermediate/leaf PEM + request.json）
├── examples/                    # 实际请求/响应 JSON 样例
├── tests/                       # 32 个 pytest 自动化用例
├── requirements.txt
├── pytest.ini
└── RUNLOG.md                    # 实际运行命令、结果与未通过项的如实记录
```

## 2. 环境与安装

* Python 3.12（3.9+ 亦可），`cryptography >= 41`。

```bash
python3 -m venv .venv && . .venv/bin/activate   # 可选
pip install -r requirements.txt
```

## 3. 生成本机测试证书链

```bash
python scripts/generate_fixtures.py
```

每个 `fixtures/<场景>/` 目录包含 `root.pem`、`intermediates.pem`、`intermediate_N.pem`、
`leaf.pem`、`leaf.key.pem`（**仅测试用私钥，本机生成，绝不可用于生产**）、
`request.json`、`scenario.txt`。

固定时间线（UTC）：

| 对象 | notBefore | notAfter |
|---|---|---|
| 根 / 中间 CA | 2020-01-01 | 2030-01-01 |
| 叶子 | 2025-01-01 | 2026-01-01 |
| 有效验证时刻 | 2025-06-01T12:00Z | |
| 过期验证时刻 | 2027-06-01T12:00Z | |

### 验收要求覆盖的链（及额外场景）

| 场景目录 | 说明 | 期望 |
|---|---|---|
| `good` | 根→中间→叶子，时间/用途/SAN 都正确 | valid |
| `expired` | 叶子已过期（notAfter 2026-01-01，在 2027-06-01 验证） | `EXPIRED` |
| `expired_intermediate` | 中间 CA 已过期 | `EXPIRED` |
| `not_yet_valid` | 叶子尚未生效 | `NOT_YET_VALID` |
| `path_length_violation` | 根 `pathLenConstraint=0` 却存在一级中间 CA | `PATH_LENGTH_VIOLATION` |
| `non_ca_intermediate` | 中间证书 `basicConstraints CA:FALSE`（无 keyCertSign）却用来签发叶子 | `NOT_A_CA` |
| `same_name_rogue_root` | **同名证书链**：伪造根与可信根 DN 完全相同但密钥不同 | `NO_PATH_TO_TRUST_ANCHOR` |
| `same_name_intermediate` | 两级中间证书同名，仅一条能链到信任根 | `NO_PATH_TO_TRUST_ANCHOR` |
| `hostname_mismatch` | 链有效但请求主机名不在 SAN | `HOSTNAME_MISMATCH` |
| `eku_mismatch` | 叶子只有 clientAuth，按 serverAuth 验证 | `EKU_MISMATCH` |
| `untrusted_root` | 链自洽但其根不在显式信任库里 | `NO_PATH_TO_TRUST_ANCHOR` |
| `wildcard` | `*.example.test` 通配符只匹配一个标签 | valid |

> **关于"同名证书链"**：`same_name_rogue_root` 中伪造根与真根的 Subject DN
> 逐字节相同、仅密钥不同。测试同时验证：只信任真根时拒绝伪造链；一旦操作员
> 误把同名伪造根加入信任库，链又会通过——以此证明信任取决于**公钥/签名**而非名字。

## 4. 启动本地服务

```bash
python -m certverifier.service --host 127.0.0.1 --port 8080
```

服务**拒绝绑定非回环地址**（`0.0.0.0` 会直接报错）。

### `POST /verify` 请求字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `leaf_certificate` | 是 | 单个叶子 PEM |
| `intermediates` | 否 | 中间证书 PEM（可多块，顺序任意） |
| `trust_anchors` | 是 | 信任根 PEM（至少一个，显式给出） |
| `verification_time` | 是 | ISO 8601 验证时刻，如 `2025-06-01T12:00:00Z` |
| `purpose` | 否 | `serverAuth`(默认) / `clientAuth` / `any` |
| `hostname` | 否 | DNS 名或 IP；不给则跳过主机名检查并在 notes 说明 |
| `options` | 否 | `check_anchor_validity` / `allow_cn_hostname` / `allow_weak_signature_algorithms` |

### curl 样例

```bash
# 有效链
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  --data @fixtures/good/request.json | jq .valid      # => true

# 过期链
curl -s -X POST http://127.0.0.1:8080/verify \
  -H 'Content-Type: application/json' \
  --data @fixtures/expired/request.json | jq '.findings[].code'   # => "EXPIRED"

# 健康检查
curl -s http://127.0.0.1:8080/health
```

也可以直接用自带脚本（stdlib，仅访问 127.0.0.1）：

```bash
python scripts/verify_request.py fixtures/good/request.json
```

**注意**：验证失败（过期、链不通等）HTTP 状态码仍是 `200`，结果看 JSON 的
`valid` 字段；只有请求本身格式错误才是 `4xx`。

### 返回体（节选）

```json
{
  "valid": false,
  "findings": [ {"code": "EXPIRED", "certificate_index": 0, "...": "..."} ],
  "chain": [ {"index": 0, "subject": "...", "sha256_fingerprint": "..."} ],
  "trust_anchor": {"subject": "...", "is_trust_anchor": true},
  "verification_time": "2027-06-01T12:00:00+00:00",
  "purpose": "serverAuth",
  "hostname": "expired.example.test",
  "revocation": {
    "checked": false,
    "crl_checked": false,
    "ocsp_checked": false,
    "note": "OFFLINE MODE: revocation status (CRL/OCSP/OCSP-stapling) was NOT checked. ..."
  }
}
```

错误码：`NO_PATH_TO_TRUST_ANCHOR`、`EXPIRED`、`NOT_YET_VALID`、
`PATH_LENGTH_VIOLATION`、`NOT_A_CA`、`MISSING_BASIC_CONSTRAINTS`、
`BAD_SIGNATURE`、`ISSUER_NAME_MISMATCH`、`LEAF_IS_CA`、
`KEY_USAGE_NO_KEY_CERT_SIGN`、`KEY_USAGE_NO_DIGITAL_SIGNATURE`、
`EKU_MISMATCH`、`HOSTNAME_MISMATCH`、`HOSTNAME_NO_SAN`、
`WEAK_SIGNATURE_ALGORITHM`、`WEAK_KEY`、`TRUST_ANCHOR_NOT_CA`。

## 5. 命令行

```bash
python -m certverifier.cli \
  --leaf fixtures/good/leaf.pem \
  --intermediates fixtures/good/intermediates.pem \
  --trust-anchor fixtures/good/root.pem \
  --verification-time 2025-06-01T12:00:00Z \
  --purpose serverAuth --hostname example.test
# 退出码：0 有效；1 验证未通过；2 输入/文件错误
```

## 6. 作为库调用

```python
from datetime import datetime, timezone
from certverifier.pemutils import parse_pem_certificates
from certverifier.verify import verify_chain
from certverifier.models import Purpose

leaf = parse_pem_certificates(open("fixtures/good/leaf.pem").read())[0]
ints = parse_pem_certificates(open("fixtures/good/intermediates.pem").read())
roots = parse_pem_certificates(open("fixtures/good/root.pem").read())

result = verify_chain(
    leaf, ints, roots,
    verification_time=datetime(2025, 6, 1, tzinfo=timezone.utc),
    purpose=Purpose.SERVER_AUTH,
    hostname="example.test",
)
print(result.valid, result.to_dict()["revocation"])
```

## 7. 测试

```bash
python -m pytest -q
```

32 个用例覆盖：四类验收链（过期 / pathLen / 非 CA 中间 / 同名）、链构建的
签名消歧、RFC 6125 主机名/通配符/IP、EKU、显式验证时刻、吊销字段恒为 false、
HTTP 服务（含拒绝绑定非回环地址）与 CLI 退出码。

## 8. 验证规则与刻意不做的事

**做的检查**（见 `certverifier/verify.py`）：链构建（名字+签名+AKI/SKI）、
签名算法（拒绝 MD5/SHA-1）、notBefore/notAfter、basicConstraints(CA/pathLen)、
keyUsage(keyCertSign/digitalSignature)、extKeyUsage、SAN 主机名/IP/通配符、
弱公钥（RSA<2048 / EC<224 位）。

**刻意不做**（离线边界）：CRL/OCSP/OCSP-staple、AIA 自动补证书、系统信任库、
nameConstraints/PolicyConstraints/AnyPolicy 等偏门 PKIX 扩展、网络抓取。
这些需要网络或在线信任决策，超出"本地安全数据处理"范围。
