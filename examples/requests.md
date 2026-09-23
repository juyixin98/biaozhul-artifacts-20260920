# HTTP 请求样例

启动服务（仅监听 127.0.0.1）：

```bash
python -m envelope_enc.cli keyring-init -k keys.json
python -m envelope_enc.server -k keys.json --token s3cret --port 8080
```

下文 `TOKEN=s3cret`。除 `/health` 外都需要请求头 `Authorization: Bearer s3cret`，
数据字段用 base64。

## 1. 健康检查（无需鉴权）

```bash
curl -s http://127.0.0.1:8080/health
# {"status": "ok"}
```

## 2. 未带 Token → 401

```bash
curl -s -w "\n[%{http_code}]\n" http://127.0.0.1:8080/keys
# {"error": "未授权：需要 Authorization: Bearer <token>"}
# [401]
```

## 3. 列出主密钥

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8080/keys
```

```json
{
  "active_kid": "mk-0001-b4a02329eb",
  "keys": [
    {"kid": "mk-0001-b4a02329eb", "status": "active",
     "created_at": "2026-09-23T17:10:39Z"}
  ]
}
```

## 4. 加密

```bash
curl -s -X POST http://127.0.0.1:8080/encrypt \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d "$(python3 -c 'import base64,json; print(json.dumps({"data_b64": base64.b64encode(b"hello "*100).decode()}))')"
```

```json
{
  "ciphertext_b64": "RU5WRU5D...（ENVENC 容器的 base64）...",
  "file_id": "f-dbe12c216dcc5079",
  "kid": "mk-0001-b4a02329eb"
}
```

可选字段 `"chunk_size": 4096`（字节，默认 65536）。

## 5. 主密钥轮换（密钥环层面）

```bash
curl -s -X POST http://127.0.0.1:8080/keys/rotate-master \
  -H "Authorization: Bearer $TOKEN" -d '{}'
```

```json
{"old_kid": "mk-0001-b4a02329eb", "new_kid": "mk-0002-443f9a08fb"}
```

注意：这一步只换密钥环；**已有密文文件仍由旧 MK1 包裹**，需要再调 `/rotate` 重包。

## 6. 把密文重包到新主密钥（只重包 DEK）

```bash
curl -s -X POST http://127.0.0.1:8080/rotate \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ciphertext_b64": "RU5WRU5D..."}'
```

```json
{
  "ciphertext_b64": "RU5WRU5D...（新头 + 原样密文块）...",
  "old_kid": "mk-0001-b4a02329eb",
  "new_kid": "mk-0002-443f9a08fb",
  "skipped": false
}
```

文件已由当前 active 主密钥包裹时 `skipped: true` 且字节不变（幂等，可安全重试）。

## 7. 解密

```bash
curl -s -X POST http://127.0.0.1:8080/decrypt \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"ciphertext_b64": "RU5WRU5D..."}'
```

```json
{"data_b64": "aGVsbG8g...", "kid": "mk-0002-443f9a08fb"}
```

## 8. 异常响应

| 场景 | 状态码 | 响应示例 |
|---|---|---|
| 缺/错 Token | 401 | `{"error": "未授权：需要 Authorization: Bearer <token>"}` |
| 字段缺失 / base64 非法 | 400 | `{"error": "缺少字段 data_b64"}` |
| 块篡改、换块、换文件 | 400 | `{"error": "第 N 块认证失败：块被篡改、块顺序被调换或来自其他文件"}` |
| 容器截断 | 400 | `{"error": "容器截断：读取第 N 块密文需要 X 字节，实际只有 Y 字节"}` |
| 头部被篡改 | 400 | `{"error": "文件头认证失败（头部字段被篡改）"}` |
| 未知路由 | 404 | `{"error": "未知接口：POST /nope"}` |
| 请求体 > 10 MiB | 413 | `{"error": "请求体超过 10485760 字节上限"}` |

## 9. Python 客户端片段

```python
import base64, json, urllib.request

BASE = "http://127.0.0.1:8080"
TOKEN = "s3cret"

def call(path, payload=None):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method="GET" if payload is None else "POST",
    )
    req.add_header("Authorization", f"Bearer {TOKEN}")
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())

enc = call("/encrypt", {"data_b64": base64.b64encode(b"hello").decode()})
call("/keys/rotate-master", {})
rot = call("/rotate", {"ciphertext_b64": enc["ciphertext_b64"]})
dec = call("/decrypt", {"ciphertext_b64": rot["ciphertext_b64"]})
assert base64.b64decode(dec["data_b64"]) == b"hello"
```
