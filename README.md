# 本地证书链验证服务

纯后端 HTTP 服务：离线验证 X.509 服务器证书链。基于 `cryptography` 的
`x509.verification` 模块（成熟验证库，Rust 实现的路径验证），核对：

- **有效期**：notBefore / notAfter（可指定验证时刻，默认当前 UTC 时间）
- **用途**：EKU 必须含 serverAuth，KeyUsage / BasicConstraints 合规
- **路径长度**：中间 CA 的 BasicConstraints pathlen 约束
- **DNS 名**：叶证书 SAN 与期望 DNS 名匹配
- **信任根**：仅使用请求中显式提供的信任根，不使用系统根

全程不访问任何在线吊销服务（无 CRL / OCSP 网络请求），完全离线。

## 环境依赖

- Python 3.12+
- 依赖见 `requirements.txt`（已锁定版本，核心为 `cryptography==50.0.1`、`fastapi==0.141.1`、`uvicorn==0.53.0`）

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动服务（默认 8000 端口）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 运行测试

```bash
.venv/bin/python -m pytest tests/ -v
```

## 生成示例 PKI 与请求样例

```bash
.venv/bin/python scripts/generate_test_pki.py
# 输出到 examples/pki/（根 CA、中间 CA、各类正常/异常证书）
# 以及 examples/request_valid.json（可直接 POST 的请求体）
```

## HTTP 接口

### `GET /health`

健康检查，返回 `{"status": "ok"}`。

### `POST /api/v1/verify`

请求体：

```json
{
  "leaf_certificate_pem": "-----BEGIN CERTIFICATE-----\n...",
  "intermediate_certificates_pem": ["-----BEGIN CERTIFICATE-----\n..."],
  "trust_roots_pem": ["-----BEGIN CERTIFICATE-----\n..."],
  "expected_dns_name": "www.example.com",
  "validation_time": "2026-09-24T00:00:00Z"
}
```

| 字段 | 说明 |
|---|---|
| `leaf_certificate_pem` | 叶证书 PEM（必填，且只能一张） |
| `intermediate_certificates_pem` | 候选中间证书 PEM 列表（可空，顺序不限，验证器自行构链） |
| `trust_roots_pem` | 显式信任根 PEM 列表（必填，至少一张） |
| `expected_dns_name` | 期望的 DNS 名（必填） |
| `validation_time` | 验证时刻，ISO 8601（可选，缺省为当前 UTC 时间） |

响应：

```json
{
  "valid": true,
  "chain_subjects": ["CN=www.example.com", "CN=Test Intermediate CA", "CN=Test Root CA"],
  "error": null
}
```

验证失败时 `valid=false`，`error` 给出原因；输入 PEM 无法解析时
`error` 以 `输入错误:` 开头。

### 请求样例（curl）

```bash
# 正常链
curl -s -X POST http://127.0.0.1:8000/api/v1/verify \
  -H 'Content-Type: application/json' \
  -d @examples/request_valid.json
```

其余场景（过期、非 CA 中间证书、名称不匹配、非信任自签）可用
`examples/pki/` 下的对应 PEM 替换 `leaf_certificate_pem` 后调用。

## 实测结果

在本机（Python 3.12.3，cryptography 50.0.1）实际运行：

- `pytest tests/ -v`：**13 passed**（正常链、过期、指定过去验证时刻、
  非 CA 中间证书、DNS 名不匹配、路径长度超限、非信任自签、缺少中间证书、
  错误信任根、畸形 PEM、空 DNS 名等场景）
- 启动 uvicorn 后用 curl 实测 5 个场景，结果与测试一致：
  - 正常链 → `valid=true`，链为 叶 → 中间 CA → 根 CA
  - 过期叶证书 → `cert is not valid at validation time`
  - 非 CA 中间证书 → `basicConstraints.cA must be asserted in a CA certificate`
  - 名称不匹配 → `leaf certificate has no matching subjectAltName`
  - 非信任自签证书 → `candidates exhausted`（不会被当作根接受）

## 未完成项 / 说明

- 仅实现服务器证书（serverAuth）验证；客户端证书（clientAuth）未提供接口，
  但 `cryptography` 的 `PolicyBuilder.build_client_verifier` 可直接扩展。
- 吊销检查刻意不做（需求要求离线、不访问在线吊销服务）；本地 CRL 文件
  校验也未实现。
- 错误信息直接透传验证库原文（英文），未做本地化映射。
