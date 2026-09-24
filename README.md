# 信封加密数据轮换服务（Envelope Encryption & Key Rotation）

纯后端 HTTP 服务：每个对象使用独立的**数据密钥（DEK）**，用 AES-256-GCM（成熟 AEAD）
加密数据，nonce 每对象唯一；DEK 由**主密钥（KEK）**包裹加密后存入 KEK 轮换时只
**重包裹 DEK**，数据密文完全不动；头部的密钥版本号纳入 AEAD 认证数据（AAD），
篡改即解密失败。任何失败都只返回错误，绝不返回部分明文。

## 对象格式 （二进制，大端）

```
magic        4B   "ENV1"      格式版本
kek_version 4B                包裹 DEK 的主密钥版本
wrap_nonce   12B               DEK 包裹的 nonce
wrapped_dek  48B               32B DEK 的 AES-256-GCM 密文（含 16B tag）
data_nonce   12B               数据密文的 nonce
ciphertext   剩余              明文的 AES-256-GCM 密文（含 16B tag）
```

- 包裹 DEK 的 AAD = `magic || kek_version` —— 头部版本号被认证，篡改头部版本号会导致解包失败。
- 数据 AAD = `magic || data_nonce` —— 认证格式版本；**刻意不含** kek_version /
  wrapped_dek，因此轮换只换头部、无需重加密数据。

## 轮换模型

- `POST /keys/rotate`：生成新版本主密钥并设为当前版本；**旧版本全部保留**。
- `POST /rewrap`：逐对象用旧 KEK 解出 DEK、用当前 KEK 重新包裹，原子替换文件。
  每个对象独立提交，中断后新旧版本混杂，但全部仍可解密；重跑 `/rewrap` 可续跑。

## 启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 锁定版本
.venv/bin/uvicorn app.main:app --port 8377
# 数据目录默认 ./data，可用 ENVELOPE_DATA_DIR 指定
```

## 请求样例

```bash
# 上传（请求体即明文）
OID=$(curl -s -X POST --data-binary '机密数据' http://127.0.0.1:8377/objects \
      | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 下载（解密返回）
curl -s http://127.0.0.1:8377/objects/$OID

# 查看头部元数据
curl -s http://127.0.0.1:8377/objects/$OID/info

# 主密钥轮换（旧版本保留）
curl -s -X POST http://127.0.0.1:8377/keys/rotate

# 重包裹所有 DEK 到当前主密钥
curl -s -X POST http://127.0.0.1:8377/rewrap

# 密钥版本列表
curl -s http://127.0.0.1:8377/keys
```

## 测试

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖验收项：

| 验收项 | 测试 |
|---|---|
| 轮换中断 | `test_interrupted_rotation_*`（核心层 + API 层模拟第 3 个对象写入时崩溃） |
| 错误密钥 | `test_wrong_key_*`、`test_wrong_key_store_cannot_decrypt` |
| 头部篡改 | `test_header_tamper_fails`、`test_kek_version_tamper_fails`、`test_tampered_header_returns_error_no_plaintext` |
| 密文截断 | `test_truncation_fails`（7 种截断位置）、`test_truncated_ciphertext_returns_error_no_plaintext` |
| 失败不返回部分明文 | 上述 API 测试断言 422 且响应体不含明文前缀 |
| 旧对象轮换后可解密 | `test_rotation_keeps_old_objects_decryptable`、`test_rewrap_rotation_only_touches_header` |

## 实测结果（2026-09-24，Python 3.12.3）

- `pytest`：**29 passed**（1 条无害警告：starlette 建议用 `httpx2` 替代 `httpx`
  驱动 TestClient，不影响功能）。
- 真实启动 uvicorn 后 curl 全流程验证通过：上传 → 轮换 → 旧对象可解密 →
  rewrap（`{"rewrapped":1}`）→ 对象仍可解密且头部变为 v2 → 篡改头部返回
  `422 DEK unwrap failed` → 截断密文返回 `422 data decryption failed`，
  响应体均不含明文。

## 未完成项 / 限制

- 主密钥以 JSON 明文存于数据目录（演示用）；生产应接入 KMS/HSM 或信封式
  外部密钥管理服务。
- 无认证/授权、无并发限流；对象整体加解密，未做分块流式（超大文件会占内存）。
- 密钥销毁（crypto-shredding）与旧版本下线未实现。
