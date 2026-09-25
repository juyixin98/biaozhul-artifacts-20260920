# 请求样例

下面的数值来自一次真实运行（`data/keys.json` 中的测试密钥每次重新生成都不同，
所以这些签名只能用于理解格式，无法在你本地直接重放）。

## 1. 被签名的 canonical request

请求：

```
POST /v1/verify HTTP/1.1
Host: 127.0.0.1:8080
X-Key-Id: test-key-1
X-Timestamp: 1790183951
X-Nonce: _V2oG901VIPqw_hFVIMzxwp2_vEpWrFN
Content-Type: application/json

{"op":"transfer","amount":42}
```

参与 HMAC 的字符串（`\n` 分行，空查询串产生一个空行；正文哈希为 hex SHA-256）：

```
POST
/v1/verify

08ce17d2daefc213e4593524d3e7c26a545215a22aaa58a16872f314245c585a
test-key-1
1790183951
_V2oG901VIPqw_hFVIMzxwp2_vEpWrFN
```

```
HMAC-SHA256(secret, canonical) =
  b1e81ef4ba3235ed450e6ad0abea982d4f06d2f66bc877f7db358c1e97b30e6d
```

## 2. HTTP 请求（curl）

```bash
curl -sS -X POST 'http://127.0.0.1:8080/v1/verify' \
  -H 'X-Key-Id: test-key-1' \
  -H 'X-Timestamp: 1790183951' \
  -H 'X-Nonce: _V2oG901VIPqw_hFVIMzxwp2_vEpWrFN' \
  -H 'Authorization: HMAC-SHA256 Credential=test-key-1, Signature=b1e81ef4ba3235ed450e6ad0abea982d4f06d2f66bc877f7db358c1e97b30e6d' \
  --data-binary '{"op":"transfer","amount":42}'
```

成功响应：

```json
{"accepted": true, "kid": "test-key-1", "nonce_prefix": "_V2o...",
 "body_sha256": "08ce17d2daefc213e4593524d3e7c26a545215a22aaa58a16872f314245c585a",
 "bytes": 30}
```

## 3. 攻击/误用样例与结果

| 场景 | 结果 |
|---|---|
| 同一请求原样再发一次（重放） | `403 {"error":"replay_detected"}` |
| 改一个字节正文，签名不变 | `401 {"error":"bad_signature"}` |
| 改方法 GET→POST 或反之 | `401 bad_signature` |
| 时间戳偏差 > 300s | `401 stale_timestamp`（±300s 边界含等号） |
| 缺少任一签名头 | `401 missing_headers` |
| nonce 含非法字符 / 短于 16 位 | `401 invalid_nonce` |
| 未知 kid | `401 unknown_key` |
| 路径写成 `/v1/./verify`、`/v0/../v1/verify`、`/v1/%76erify` | 规范化为 `/v1/verify`，同一签名通过 `200` |
| 用 `/v1%2Fverify`（编码斜杠）冒充 `/v1/verify` | `404`（编码斜杠不还原为分隔符），用真斜杠路径的签名访问则 `401 bad_signature` |
| 40 个线程同时发同一签名请求 | 恰好 1 个 `200`，39 个 `403 replay_detected` |

## 4. 自动生成并发送

```bash
# 打印 canonical 串、签名头和可复制的 curl 命令；加 --send 立即发送
echo '{"op":"ping"}' | python3 -m scripts.sign_request \
  --url http://127.0.0.1:8080/v1/verify --kid test-key-1 --send

# 完整现场验收脚本（含 40 并发重复请求）
PYTHONPATH=. python3 examples/acceptance_live.py
```
