# 离线证书链验证服务（offline-cert-verify）

纯后端、纯本地的 X.509 证书链验证服务。基于成熟密码库
[`cryptography`](https://cryptography.io/) 的 `x509.verification` 模块
（RFC 5280 风格路径构建与验证）实现，**不自创任何密码算法或验证算法**。

## 安全边界（务必阅读）

- **完全离线**：服务不发起任何网络请求 —— 不抓 AIA 证书、不查 CRL、不查 OCSP。
- **吊销状态未验证**：因为不联网也不离线加载 CRL/OCSP，链中所有证书的吊销状态
  **未知**。每个响应都带 `revocation_checked: false` 和 `revocation_note` 明示。
  如需吊销语义，调用方必须自行离线提供吊销信息并另行核查。
- **显式输入**：信任根、中间证书、叶子证书、验证时刻、用途（purpose）、
  主机名（hostname）全部由调用方显式提供，无任何隐式默认值。
- **测试密钥本地生成**：`fixtures/` 与测试中的密钥均为本地临时生成的 RSA-2048
  密钥，不对应任何真实主体，不接入任何生产账号或生产 PKI。
- 服务默认仅监听 `127.0.0.1`；请求体上限 1 MiB。
- `validation_time` 为 ISO 8601；缺省时区按 **UTC** 处理。

## 环境准备

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # cryptography>=42, pytest
```

## 运行服务

```bash
.venv/bin/python -m certchain.service --host 127.0.0.1 --port 8443
```

接口：

| 方法 | 路径      | 说明                 |
| ---- | --------- | -------------------- |
| GET  | `/health` | 健康检查             |
| POST | `/verify` | 证书链验证（见下）   |

### 请求样例

`examples/verify-request.json`（完整样例，证书为 PEM 字符串）：

```json
{
  "leaf": "-----BEGIN CERTIFICATE-----\n...",
  "intermediates": ["-----BEGIN CERTIFICATE-----\n..."],
  "trust_roots": ["-----BEGIN CERTIFICATE-----\n..."],
  "validation_time": "2026-06-01T00:00:00Z",
  "purpose": "server_tls",
  "hostname": "service.example.local"
}
```

- `purpose`: `server_tls`（必须给 `hostname`，校验 SAN 与 serverAuth EKU）
  或 `client_tls`（校验 clientAuth EKU）。
- 响应：`valid` / `chain`（叶子→根的有序链）/ `error` /
  `validation_time` / `purpose` / `hostname` /
  `revocation_checked: false` / `revocation_note`。
  输入不合法返回 HTTP 400。

调用：

```bash
curl -s -X POST http://127.0.0.1:8443/verify \
  -H 'Content-Type: application/json' \
  -d @examples/verify-request.json
```

成功响应样例见 `examples/good-response.json`。

## 验收用例

`scripts/gen_fixtures.py` 在本地生成全部验收用例的证书链与请求体：

```bash
.venv/bin/python scripts/gen_fixtures.py fixtures
```

| 用例目录                       | 构造                                             | 预期结果 |
| ------------------------------ | ------------------------------------------------ | -------- |
| `fixtures/good/`               | root(pathlen=1) → intermediate → leaf            | 通过     |
| `fixtures/expired/`            | 叶子 not_after=2021-01-01，验证时刻 2026-06-01   | 拒绝（过期） |
| `fixtures/pathlen_violation/`  | root(pathlen=0) 下挂两级中间 CA                  | 拒绝（路径长度违规） |
| `fixtures/non_ca_intermediate/`| 中间证书 BasicConstraints ca=False 仍签发叶子    | 拒绝（非 CA 中间证书） |
| `fixtures/same_name/`          | 同名异钥的双根、双中间 CA；叶子由 #1 体系签发    | 给出全部中间证书时按密钥选中正确路径，通过；只给同名错误中间证书（`request-wrong-intermediate.json`）时拒绝 |

所有用例的响应均显式声明 **吊销状态未验证**（`revocation_checked: false`）。

## 自动化测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：正常链、过期、路径长度违规、非 CA 中间证书、同名链正/反向、
主机名不匹配、不受信根、用途（EKU）不匹配、显式验证时刻前后对照、
输入校验（缺 validation_time / hostname / 非法 PEM / 空信任根）、
HTTP 端到端（真实起服务发请求）。

## 实际运行记录（2026-09-24，本机 Ubuntu / Python 3.12.3）

环境准备：

```
$ python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
# 实际安装：cryptography 50.0.1, pytest 9.1.1
# 注：系统自带 cryptography 41.0.7 无 x509.verification 模块（42.0 引入），
#     且系统 Python 为 PEP 668 管理环境，故使用 venv。
```

测试：

```
$ .venv/bin/python -m pytest tests/ -v
============================== 26 passed in 7.89s ==============================
```

服务端到端（`python -m certchain.service --port 8443` 后台运行，curl 逐个 POST）：

| 用例 | valid | error |
| ---- | ----- | ----- |
| good | true | null（链长 3，revocation_checked=false） |
| expired | false | `cert is not valid at validation time` |
| pathlen_violation | false | `candidates exhausted: path length constraint violated` |
| non_ca_intermediate | false | `candidates exhausted: basicConstraints.cA must be asserted in a CA certificate` |
| same_name | true | null（同名中间证书并存时按密钥选中正确路径） |
| same_name（仅错误同名中间证书） | false | `candidates exhausted: signature does not match` |

未通过项：无（首轮测试曾因夹具叶子证书有效期过短、缺 AuthorityKeyIdentifier
扩展导致 5 个用例失败，修正夹具后全部通过；详见 git 历史）。

## 目录结构

```
certchain/
  verifier.py    # 核心验证逻辑（封装 cryptography.x509.verification）
  fixtures.py    # 本地测试 PKI 生成（验收用例链）
  service.py     # HTTP 服务（仅标准库，默认监听 127.0.0.1:8443）
scripts/gen_fixtures.py   # 生成 fixtures/ 下各用例的 PEM 与请求样例
tests/                    # pytest 自动化测试
examples/                 # 请求/响应样例
fixtures/                 # 生成的验收用例证书链（可随时重新生成）
```
