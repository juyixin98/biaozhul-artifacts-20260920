# 离线 X.509 证书链验证服务（Python + FastAPI + cryptography）

纯后端 HTTP 服务：输入 **叶证书 + 候选中间证书 + 显式信任根**，在**完全离线**
（不访问任何 OCSP/CRL/AIA/网络服务）的条件下完成 X.509 证书链验证。

信任判定由成熟验证库完成：[`cryptography`](https://cryptography.io/) 包的
`cryptography.x509.verification` 模块（cryptography ≥ 42，底层为 Rust 编写的
webpki/rustls 验证引擎）。本项目自己的代码只负责参数解析、调用引擎，以及把引擎
结论整理成结构化 JSON；**不自行实现签名/路径等密码学判定**。

---

## 1. 检查内容

引擎（权威判定）强制执行：

| 检查项 | 说明 |
|---|---|
| 签名校验 | 叶证书→中间证书→根，每一环的密码学签名都被验证 |
| 有效期 | 叶证书、**每一级中间证书、信任根本身** 的 notBefore/notAfter |
| 基本约束 | 中间证书必须有 `BasicConstraints.cA=TRUE` |
| 路径长度 | `pathLenConstraint` 约束 + 可选的 `max_chain_depth` 硬上限 |
| 名称链接 | issuer/subject、AKI/SKI 匹配，路径构建 |
| 扩展合规 | RFC 5280 / WebPKI 要求的扩展（KeyUsage、SKI/AKI、末端 SAN 等） |
| 密钥用途 | `ExtendedKeyUsage` 与用途一致（serverAuth / clientAuth） |
| DNS/IP 名称 | 请求的主机名（支持 RFC 6125 左标签通配）或 IP 与叶证书 SAN 匹配 |
| **信任锚** | 链**只能**终止于请求中显式给出的 `trust_roots`；候选中间证书里的自签证书**永远不会**被当作根接受 |

**不做（也明确不支持）**：

- ❌ 在线吊销（OCSP / OCSP-stapling / CRL / CRLite）——完全离线，无任何网络出站；
- ❌ 系统/浏览器信任库——信任根必须由调用方显式提供，空信任库直接报错；
- ❌ 名称回退到 CN（WebPKI 规则，只看 SAN）。

---

## 2. 依赖与启动

### 环境

- Python 3.10+（开发实测：Python **3.12.3**，Linux x86_64）
- 依赖见 `requirements.txt`（宽松约束）与 **`requirements.lock`**（开发环境实际锁定版本）

### 安装

```bash
python3 -m venv .venv
# 复现已锁定的版本：
.venv/bin/pip install -r requirements.lock
# 或按宽松约束安装：
# .venv/bin/pip install -r requirements.txt
```

### 启动服务

```bash
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动后：

- 健康检查：`GET http://127.0.0.1:8000/health`
- 交互式接口文档（Swagger UI）：`http://127.0.0.1:8000/docs`
- OpenAPI：`http://127.0.0.1:8000/openapi.json`

### 生成示例证书

```bash
.venv/bin/python scripts/gen_certs.py        # 在 examples/certs/ 下生成全套本地 CA 与夹具
bash scripts/run_examples.sh                 # 向运行中的服务逐个发送 examples/requests/*.json
```

---

## 3. HTTP 接口

### `POST /v1/verify`

**请求体（JSON）**

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `leaf_certificate` | string | 是 | 叶证书，PEM（也接受 DER 或纯 base64） |
| `intermediate_certificates` | string[] | 否 | 候选中间 CA 证书；每项可为 PEM/DER/base64，**PEM 也可含多个证书块（bundle）**。非信任，仅用于路径构建；多余证书会被忽略 |
| `trust_roots` | string[] | 是 | 显式信任根，至少 1 个；**只有这些自签根可以终止信任链** |
| `purpose` | string | 否 | `server_auth`（默认）或 `client_auth` |
| `subject` | string | server_auth 必填 | 期望的主机名（DNS，支持 SAN 通配）或 IP 字面量，如 `example.com`、`10.0.0.5` |
| `verification_time` | string | 否 | ISO 8601 **带时区**时间（如 `2026-09-24T12:00:00Z`），缺省为当前 UTC |
| `max_chain_depth` | int ≥ 0 | 否 | 链深度硬上限（中间 CA 数量） |

**返回（HTTP 恒为 200，结论看 `valid`）**

- `valid`（bool）：引擎的最终结论；
- `error_code` / `error_message`：失败时的稳定错误码与引擎原文；
- `chain`：按 叶→中间→根 排列的诊断视图（每级含 subject/issuer、有效期、CA、SAN、EKU、SHA-256 指纹等）；
- `findings`：独立的诊断条目（过期、名称不匹配、非 CA、路径长度、EKU 等），**仅供参考**，不会推翻引擎结论。

错误码：`VALIDITY_PERIOD`、`NAME_MISMATCH`、`NOT_A_CA`、`PATH_LEN_CONSTRAINT`、
`MAX_CHAIN_DEPTH`、`EKU`、`REQUIRED_EXTENSION`、`UNTRUSTED_CHAIN`、
`VERIFICATION_FAILED`。

参数错误返回 `400 {"error":"invalid_request",...}`；证书无法解析返回
`422 {"error":"certificate_parse_error",...}`。

### 请求样例（curl）

```bash
curl -sS -X POST http://127.0.0.1:8000/v1/verify \
  -H 'Content-Type: application/json' \
  --data-binary @examples/requests/01_valid.json | jq .
```

### 最小内联请求体

```json
{
  "leaf_certificate": "-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----",
  "intermediate_certificates": ["-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"],
  "trust_roots": ["-----BEGIN CERTIFICATE-----\n...\n-----END CERTIFICATE-----"],
  "purpose": "server_auth",
  "subject": "demo.example.com"
}
```

`examples/requests/` 下另含 9 个可直接发送的完整请求体；`examples/output/` 是
本次实测的完整响应留档。

---

## 4. 验收场景与实测结果

测试夹具全部由本地 CA（`scripts/gen_certs.py` / `tests/certfactory.py`，ECDSA
P-256，另含 RSA 2048 用例）现造，时间点相对"现在"生成，保证用例长期有效。
下列结果为**本机真实运行所得**。

### 4.1 自动化测试

```bash
.venv/bin/python -m pytest
# 65 passed, 1 warning in 0.66s
```

覆盖：有效链（EC/RSA、通配、IP SAN、clientAuth、历史时间点）、叶/中间/根过期、
未来生效、非 CA 中间证书、`pathLen=0/1`、DNS 名称不匹配（含通配越界）、EKU 不符、
深度限制、非信任自签、攻击者根仅作为"中间证书"提交、缺中间证书、PEM/DER/base64/
bundle 解析、输入校验、HTTP 状态码与响应结构。

### 4.2 真实 HTTP 示例（对运行中的 uvicorn 实发请求）

| 请求文件 | 场景 | `valid` | `error_code` |
|---|---|---|---|
| `01_valid.json` | 根→中间→叶，名称匹配 | `true` | — |
| `02_expired.json` | **过期叶证书** | `false` | `VALIDITY_PERIOD` |
| `03_non_ca_intermediate.json` | **非 CA 中间证书**（cA=false） | `false` | `NOT_A_CA` |
| `04_name_mismatch.json` | **名称不匹配**（SAN 为 other.example.net，请求 demo.example.com） | `false` | `NAME_MISMATCH` |
| `05_untrusted_selfsigned.json` | **非信任自签证书**，信任库是另一张根 | `false` | `UNTRUSTED_CHAIN` |
| `06_selfsigned_root_as_intermediate.json` | 攻击者自签根**仅放在 intermediate_certificates 里** | `false` | `MAX_CHAIN_DEPTH`¹ |
| `07_client_auth_ok.json` | clientAuth 叶证书按 client 用途验证 | `true` | — |
| `08_eku_mismatch.json` | clientAuth 叶证书按 server 用途验证 | `false` | `EKU` |
| `09_historical_time_ok.json` | 用 `verification_time` 指定到证书仍有效的过去时刻 | `true` | — |

¹ 引擎不会把中间证书列表中的自签根当锚点：路径构建到该证书后无法继续链接到
`trust_roots` 中的真正信任根，在 WebPKI 深度规则下表现为
`chain construction exceeds max depth`。等价的安全结论——**自签证书没有进入信任
库就不能终止链**——同时由单元测试
`test_self_signed_root_in_intermediates_is_not_anchor` 固定。

复现命令：

```bash
.venv/bin/python scripts/gen_certs.py
.venv/bin/uvicorn app.main:app --port 8000 &      # 或换一个空闲端口
bash scripts/run_examples.sh http://127.0.0.1:8000
```

---

## 5. 项目结构

```
.
├── app/
│   ├── main.py          # FastAPI 路由、请求模型、错误处理
│   ├── verifier.py      # 封装 cryptography.x509.verification 的权威验证
│   └── pki.py           # PEM/DER 解析、证书摘要、诊断性路径/有效期/名称检查
├── tests/
│   ├── certfactory.py   # 本地 CA / 证书构造工厂
│   ├── conftest.py      # 有效链及各类"故意破坏"的夹具
│   ├── test_verifier.py # 验收场景（验证核心）
│   ├── test_api.py      # HTTP 端到端
│   └── test_pki.py      # 解析与通配规则单元测试
├── scripts/
│   ├── gen_certs.py     # 生成 examples/certs 下的演示 PKI
│   └── run_examples.sh  # 批量发送示例请求
├── examples/
│   ├── certs/           # 生成的证书（含 *.key，已被 .gitignore 忽略）
│   ├── requests/        # 9 个可直接 POST 的 JSON 请求体
│   └── output/          # 本次实测的完整响应留档
├── requirements.txt     # 宽松依赖约束
├── requirements.lock    # 锁定依赖（实际安装版本，pip freeze）
└── pytest.ini
```

---

## 6. 设计说明与安全边界

1. **信任根必须显式给出。** 服务不读系统证书库；`trust_roots` 为空在进入引擎前
   即被拒绝（引擎本身也禁止空 Store）。
2. **中间证书是非信任输入。** 它们只参与路径构建；引擎会自行选择所需证书，
   多余/无关证书被忽略，放在其中的自签根不具备锚点地位。
3. **诊断信息与权威判定分离。** `chain`/`findings` 由本项目尽力重建，仅用于解释；
   `valid` 永远只来自 webpki 引擎。
4. **无网络。** 代码库中没有任何 HTTP 客户端、URL 获取或吊销逻辑；证书中的 AIA/
   CRL Distribution Points 扩展不会被使用。
5. **时间可显式注入**（`verification_time`，必须带时区），便于审计/复算历史结论；
   缺省为当前 UTC。
6. 该引擎按 **WebPKI** 规则验证（要求末端 SAN、强制较严格的扩展集），因此对于
   只有 CN、没有 SAN 的老式证书会判 `REQUIRED_EXTENSION`/`NAME_MISMATCH`——这是
   预期行为，不是缺陷。

## 7. 未完成项 / 已知限制

- **未做吊销检查**：任务明确要求离线、不访问在线吊销服务，故无 OCSP/CRL。如需
  吊销能力，可在**离线**前提下另行支持"本地 CRL 文件/本地吊销列表"注入，当前未实现。
- 未做认证、限流、TLS 终止等运维措施；按纯后端本地服务定位，建议仅监听
  `127.0.0.1` 或置于内网/网关之后。
- 信任根是否"自签"不做强制要求：引擎按 WebPKI 语义信任 Store 中给定的锚
  （允许"锚定在中间"的配置）；调用方应只放入自己真正信任的根。
- `examples/certs/*.key` 仅为本地演示夹具，**不可用于任何真实用途**，且已在
  `.gitignore` 中忽略。
