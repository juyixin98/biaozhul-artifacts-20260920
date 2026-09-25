# Anti-Replay Request Verification

本地安全数据处理服务，演示基于 HMAC 的请求签名验证与防重放机制。

## 安全边界

- 签名原语仅使用 Python 标准库 `hmac` + `hashlib`（HMAC-SHA256，底层 OpenSSL），**不自创加密算法**。
- 密钥本地生成（`secrets`），仅用于测试/本地演示，**不接任何生产账号**。
- 密钥文件仅在启动时读入内存；**日志在输出层统一脱敏**，任何模块都不得记录密钥、签名或完整 `Authorization` 头。
- 本服务不提供 TLS；生产部署必须位于 TLS 终端（网关/反向代理）之后，签名只能作为防篡改 + 防重放层。

## 签名覆盖范围

签名串按行拼接以下字段，全部参与 HMAC：

1. HTTP 方法（大写）
2. 规范路径（canonical path，见下）
3. 规范查询串（canonical query，见下）
4. 正文 SHA-256 摘要（空正文也参与）
5. 密钥 ID（`kid`）
6. Unix 时间戳（秒）
7. nonce

签名头：

| 头 | 说明 |
|---|---|
| `X-Key-Id` | 密钥 ID |
| `X-Timestamp` | Unix 秒，整数 |
| `X-Nonce` | 16–128 字符，`[A-Za-z0-9_\-]` |
| `Authorization` | `HMAC-SHA256 Credential=<kid>, Signature=<hex>` |

## 编码规范（canonicalization）

- **路径**：按 `/` 切分 → 拒绝非法百分号编码（`%` 后必须跟两位十六进制）→ 逐段百分号解码 → 拒绝控制字符与空字节 → RFC 3986 重新编码（非 `unreserved` 字符统一大写 `%XX`）→ 做 `.`/`..` 归一化。因此 `/a/./b`、`/a/c/../b`、`/a%2fb`（编码斜杠）、`/a%2Fb`、`/A` 与 `/a` 等歧义写法会被规范化或明确拒绝，服务器与签名方得到的路径恒等。
- **查询串**：按 `&` 拆键值（无 `=` 时值为空串），键、值各自百分号解码再重编码，按 `(key, value)` 字典序排列后以 `k=v&...` 重组。
- 编码差异（大小写 `%2f`/`%2F`、`+` 与 `%20`）在重编码后消失，避免同一路径/参数产生不同签名。

## 重放窗口与 nonce 原子登记

- 时间窗口：`|server_time - X-Timestamp| <= 300` 秒，边界**含等号**。
- nonce 在窗口内不可重复。登记由 SQLite `INSERT OR FAIL`（单列 `UNIQUE` 约束 + `NOT NULL` 到期时间）完成：**检查与插入是单条原子语句**，并发下数据库约束保证同一 (kid, nonce) 只有一次插入成功，其余收到 `403 replay_detected`。
- 过期 nonce（`expires_at <= now`）由后台守护线程周期清理；插入前也会顺手清理本 kid 的过期行。
- 验证顺序：头格式 → 时间窗口 → HMAC（常量时间比较 `hmac.compare_digest`）→ nonce 原子登记。签名非法的请求不会占用 nonce 名额。

## 快速开始

```bash
# 1) 生成测试密钥（本地写入 keys.json，权限 0600）
python3 -m scripts.gen_keys

# 2) 启动服务（默认 127.0.0.1:8080）
python3 -m replay_protection.server

# 3) 另开终端：用签名脚本发请求
echo '{"op":"ping"}' | python3 -m scripts.sign_request \
    --url http://127.0.0.1:8080/v1/verify --method POST --kid test-key-1
```

`GET /healthz` 无需签名。`POST /v1/verify` 为示例受保护资源，原样回显收到的正文摘要与 nonce。

## 自动化测试

```bash
python3 -m unittest discover -s tests -v
```

覆盖：并发重复请求只接受一次（线程级 + 进程级竞态）、路径编码歧义、时间边界（±300s 含端点及越界）、正文篡改、查询串歧义、缺头/坏头、未知密钥、nonce 过期后可复用、日志脱敏（密钥/签名不出现）。

## 目录

```
replay_protection/   服务与验证库
scripts/             gen_keys（密钥生成）、sign_request（签名客户端/样例）
tests/               自动化测试
examples/            签名串与 curl 请求样例
RUNLOG.md            实际运行命令与结果记录
```
