# anti-replay

本地安全数据处理服务的 **防重放请求验证** 纯后端实现。

每个受保护请求都携带 HMAC-SHA256 签名，签名内容覆盖 **HTTP 方法、规范路径、规范查询串、
正文摘要、时间戳、nonce**；服务端在有限的时间窗口内校验签名，并对 nonce 做
**原子登记**：并发的重复请求只有一个被接受，其余得到 `409 replay_detected`。

- 密码原语全部来自成熟库 [`cryptography`](https://cryptography.io/)（HMAC/SHA-256），
  测试密钥由 OS CSPRNG（`secrets`）本地生成，**不连接任何生产账号，不自创加密算法**。
- 无前端、无外部服务依赖；HTTP 层仅使用 Python 标准库。

## 目录结构

```
.
├── README.md                  # 本文档（协议规范 + 运行说明）
├── requirements.txt
├── run_tests.sh               # 自动化测试入口
├── src/anti_replay/
│   ├── crypto.py              # HMAC / SHA-256 / 随机密钥（cryptography + secrets）
│   ├── canonical.py           # 规范请求编码（路径/查询串归一化）
│   ├── signing.py             # 签名串构造、签名与校验
│   ├── nonce_store.py         # nonce 登记：内存版 + SQLite 版（原子 claim）
│   ├── verifier.py            # 完整验证流水线（头解析→签名→时间窗→nonce）
│   ├── keys.py                # 本地 keystore 加载/生成（强制 0600 权限）
│   ├── logutil.py             # 日志配置 + 密钥脱敏过滤器
│   └── server.py              # ThreadingHTTPServer 服务 / CLI 入口
├── scripts/keygen.py          # 生成本地测试密钥
├── examples/
│   ├── client.py              # 签名客户端 + 完整攻击场景演示
│   ├── run_demo.sh            # 起服务并跑全部场景
│   ├── requests.http          # 线上报文样例（原始 HTTP 文本）
│   └── README.md
└── tests/                     # unittest 自动化测试
```

## 快速开始

需要 Python 3.10+，安装依赖：

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
```

（系统已安装兼容版本的 `cryptography` 时也可直接用系统 Python。）

生成测试密钥并启动服务：

```bash
python3 scripts/keygen.py --keystore dev-keys.json --key-id demo-key-1
PYTHONPATH=src python3 -m anti_replay.server --keystore dev-keys.json --port 8080
```

另开终端运行端到端演示（正常请求、重放、并发重复、正文篡改、时间边界、路径编码歧义）：

```bash
PYTHONPATH=src python3 examples/client.py demo --base-url http://127.0.0.1:8080 \
    --keystore dev-keys.json
```

或一键演示（自动起停服务）：

```bash
examples/run_demo.sh
```

## 运行测试

```bash
./run_tests.sh
# 等价于：PYTHONPATH=src python3 -m unittest discover -s tests -v
```

实测命令与结果记录见 [`docs/run-record.md`](docs/run-record.md)。

## HTTP 接口

| 方法 | 路径 | 是否签名 | 说明 |
|---|---|---|---|
| GET | `/health` | 否 | 存活探针，返回 `{"status":"ok"}` |
| POST | `/api/data` | 是 | 接收 JSON 正文，校验通过后返回 `{"status":"accepted"}` |

任何 POST 到 `/api/*` 的请求都必须携带下列请求头：

| 请求头 | 格式 | 说明 |
|---|---|---|
| `X-Auth-Key-Id` | `[A-Za-z0-9_-]{1,64}` | 服务端据此查找密钥 |
| `X-Auth-Timestamp` | Unix 秒，纯 ASCII 十进制整数 | 与服务端时间偏差不得超过窗口（默认 ±300 秒） |
| `X-Auth-Nonce` | `[A-Za-z0-9_-]{8,128}` | 单次随机字符串，窗口内不可复用 |
| `X-Auth-Signature` | 64 位小写十六进制 | HMAC-SHA256，见下 |

错误响应统一为 `4xx` + JSON：

```json
{"error": {"code": "replay_detected", "message": "nonce has already been used"}}
```

| HTTP | code | 触发条件 |
|---|---|---|
| 400 | `malformed_request` | 缺少/格式非法的认证头、重复或非法 `Content-Length`、请求目标无法规范化 |
| 400 | `invalid_json` | 签名通过但 POST 正文不是 UTF-8 JSON 对象 |
| 401 | `unknown_key` | key id 不存在 |
| 401 | `bad_signature` | HMAC 校验失败（含正文/路径/查询串/时间戳/nonce 被篡改） |
| 401 | `stale_timestamp` / `future_timestamp` | 时间戳超出 ±窗口 |
| 409 | `replay_detected` | nonce 在窗口内已被登记 |
| 411 | `length_required` | POST/PUT 缺少 `Content-Length` |
| 413 | `payload_too_large` | 正文超过 1 MiB 上限 |
| 404 | `not_found` | 路由不存在 |

## 签名协议 v1

### 1. 规范请求串

按固定顺序以 `\n`（LF，无尾换行）拼接以下 8 行：

```
ANTI-REPLAY-API-HMAC-SHA256-v1
<key_id>
<METHOD>                       # 大写
<canonical-path>
<canonical-query>              # 无查询串时为空行
<body-sha256-hex>              # 正文（原始字节）SHA-256 的小写十六进制；空正文为 sha256(b"")
<timestamp>                    # 与 X-Auth-Timestamp 完全一致
<nonce>                        # 与 X-Auth-Nonce 完全一致
```

`signature = hex(HMAC_SHA256(key, utf8(canonical-request)))`。

### 2. 路径规范化（消除路径编码歧义）

对 raw path（`?` 之前部分，必须以 `/` 开头）：

1. 按 `/` 分段，逐段做**严格百分号解码**：
   - 每个 `%` 后必须跟两位十六进制数，否则拒绝；
   - 原始路径只允许 ASCII，非 ASCII 字符必须以 UTF-8 百分号编码；
   - 解码后若包含 NUL/C0 控制字符，**拒绝**；
   - 解码后若包含 `/`（即 `%2f`、`%2F` 这类编码分隔符），**拒绝**——
     一个段不允许通过编码“变成”两个段；
2. 点段归一：`.` 丢弃；`..` 弹出上一段；`..` 试图逃出根（如 `/../x`）时**拒绝**；
3. 空段折叠（`//` 等价于 `/`）；
4. 每段以严格白名单重新百分号编码：未转义字符仅 `A-Z a-z 0-9 - . _ ~`，
   其余字节一律大写 `%HH`（含空格→`%20`、UTF-8 多字节序列）。

因此下列请求目标规范化后**完全相同**（签名一致、等价被接受）：

```
/api/data
/./api/data
//api//data
/api/%64ata
/api/../api/data
```

而下列请求一律拒绝（`400 malformed_request`）：

```
/api%2fdata        # 编码的路径分隔符
/../etc/passwd     # 逃出根的点段
/api/%00           # 控制字符
```

### 3. 查询串规范化

- 无查询串（及空串）→ 空行；
- 每个参数必须形如 `key=value`（不允许无 `=` 的裸标记、不允许空键）；
- `+` 就是字面加号（本协议不是 form-urlencoded，空格必须写 `%20`）；
- 键、值按与路径相同的规则做严格百分号解码（控制字符拒绝），再以白名单重编码；
- **重复键拒绝**；
- 按 `(解码后的键 UTF-8 字节, 值 UTF-8 字节)` 字典序排序后用 `&` 连接。

`?b=2&a=1` 与 `?a=1&b=2` 规范结果相同；`?a=1&a=2` 拒绝。

### 4. 时间窗口与 nonce 原子登记

- 窗口默认 300 秒，双向：`abs(server_now - timestamp) <= 300` 才通过
  （边界等号成立：恰好在 ±300s 上通过，±301s 拒绝）。
- 验证分两阶段（见 `Verifier.authenticate()` 与 `Verifier.commit_nonce()`）：
  1. **认证**（不触碰 nonce 表）：认证头格式 → 密钥查找 → 规范编码 → HMAC → 时间窗；
  2. 调用方完成所有可能失败的前置工作（本服务为正文 JSON 解析/校验）；
  3. **nonce 原子登记**：在请求“生效”前的最后一步执行；
  4. 生效（本服务即回显正文摘要）。

  这样任何发生在“生效”之前的失败（正文非法、下游瞬时错误）都**不会烧掉 nonce**，
  诚实客户端改正后仍可在窗口内重试；而并发重复请求在第 3 步被唯一裁决。
- nonce 以 `(key_id, nonce)` 为作用域；登记操作是原子的：
  - 内存存储：单一 `threading.Lock`，锁内完成过期清理 + “已存在则拒绝，否则写入”；
  - SQLite 存储（`--store sqlite`）：`(key_id, nonce)` 主键 + `BEGIN IMMEDIATE`
    事务，靠唯一约束保证跨线程/跨进程只有一个插入成功。
- nonce 记录保留 `2 × window` 后清理；保留期 ≥ 重放有效期，因此窗口内重放必然命中登记表，
  窗口外重放则先被时间窗拒绝。两道防线没有空档。

### 5. 安全注意

- HMAC 校验使用 `cryptography` 的恒定时间比较。
- **日志不记录密钥与签名**：认证相关日志只含 key id、对等地址、拒绝原因和
  nonce 的短指纹（SHA-256 前 8 位）；另有 `RedactingFilter` 对已知密钥字符串做二次脱敏。
- 密钥文件强制 `0600` 权限，权限过宽时服务拒绝启动。
- 正文上限 1 MiB（`Content-Length` 超限返回 413）。
- 内存 nonce 存储是单进程方案；多进程/多机部署应使用 SQLite 存储或外部共享存储，
  否则各实例各自去重。

## 服务端命令行

```bash
PYTHONPATH=src python3 -m anti_replay.server \
  --keystore dev-keys.json \
  --host 127.0.0.1 --port 8080 \
  --window 300 \
  --store memory          # 或 sqlite --db nonces.db
```

## 手动调用（curl 思路）

curl 无法直接计算 HMAC，请用 `examples/client.py call` 发送单个真实请求：

```bash
PYTHONPATH=src python3 examples/client.py call --base-url http://127.0.0.1:8080 \
  --keystore dev-keys.json --method POST --path /api/data \
  --body '{"hello":"world"}'
```

原始报文与各攻击场景样例见 [`examples/requests.http`](examples/requests.http)。
