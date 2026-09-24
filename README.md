# 制品签名与信任轮换服务（纯后端）

用 Python + FastAPI + `cryptography`（Ed25519 / RFC 8032）实现的本地制品签名验证服务：

- **签名绑定**：每个制品签名都绑定 `SHA-256(正文) + 制品类型 + 版本 + 一次性 nonce`，
  正文篡改、跨类型搬用、跨版本搬用都会验签失败；
- **防重复 / 防重放**：每个 nonce 只允许消费一次；同一 `类型@版本` 只允许登记一次；
- **拒绝版本回退**：同一制品类型的版本必须严格按 `MAJOR.MINOR.PATCH` 递增；
- **信任根轮换**：新根必须由**当前旧根的阈值成员**（threshold-of-N）签名批准，
  批准签名覆盖新根的全部内容（新版本号、新阈值、新成员公钥集合）；根版本也必须严格递增；
- **失效根 / 失效密钥**：轮换后旧阈值成员、旧制品签名者立即失去授权，其新签名被拒，
  旧制品验签时密码学签名仍可验证，但 `signer_trusted=false`、整体 `valid=false`。

服务本身**不持有任何私钥**：签名在客户端（构建方）离线完成，服务只做登记与验签。
本仓库全部密钥均为临时测试密钥（由脚本现生成），**不包含、也不应使用任何生产凭据**。

---

## 1. 依赖与启动

要求 Python 3.10+（开发实测 3.12.3，Linux x86_64）。

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.lock          # 锁定依赖（含传递依赖，见文件头注释）
# 或宽松安装：pip install -r requirements.in

# 启动（纯内存态，进程退出数据消失）
uvicorn app.main:app --reload --port 8000

# 启动并持久化到 JSON（原子写）
STATE_PATH=data/state.json uvicorn app.main:app --port 8000
```

- 直接依赖：`fastapi`、`uvicorn[standard]`、`cryptography`；测试/演示额外用 `pytest`、`httpx`。
- 完整锁定版本见 [`requirements.lock`](requirements.lock)（由 `pip freeze` 生成）。
- 交互式 API 文档：启动后访问 <http://127.0.0.1:8000/docs>（Swagger UI）。

生成一整套测试密钥（输出到 `dev-keys/`，该目录已被 git 忽略）：

```bash
python scripts/gen_test_keys.py dev-keys
# root_threshold_keys.json(3)  signer_keys.json(2)
# new_threshold_keys.json(2)   new_signer_keys.json(1)
```

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/healthz` | 健康检查 |
| POST | `/root/init` | 初始化首个信任根（只能一次） |
| POST | `/root/rotate` | 旧根阈值签名批准，轮换到新根 |
| GET  | `/root` | 查看当前信任根 |
| POST | `/artifacts/sign` | 登记一份已离线签名的制品（服务端验签 + 防重放 + 防回退） |
| POST | `/artifacts/verify` | 验签（支持已登记制品 / 调用方自带公钥两种模式） |
| GET  | `/artifacts` | 列出已登记制品 |

错误统一为 `{"detail": "..."}`，常用状态码：`400` 字段/签名无效、`403` 无权（非阈值成员、
非授权签名者、批准不足）、`404` 制品未登记、`409` 未初始化根 / 重复 / 版本或根版本回退。

### 2.1 客户端如何离线构造签名

```python
from app import crypto

priv = "<构建方 Ed25519 私钥 hex，32 字节种子>"
digest = crypto.sha256_hex(b"制品正文")          # 64 hex
nonce  = crypto.generate_nonce()                 # 32 hex（16 字节随机）
sig = crypto.sign_artifact(
    private_key_hex=priv,
    digest_hex=digest,
    artifact_type="firmware",
    version="1.0.0",
    nonce_hex=nonce,
)
# POST /artifacts/sign 体：
# {artifact_type, version, digest, key_id, nonce, signature}
```

被签名消息采用长度前缀 + 域分离前缀的明确编码（`app/crypto.py`），
制品签名前缀 `ARTIFACT-SIGNATURE/v1` 与根轮换前缀 `ROOT-ROTATION/v1` 互不相同，
从结构上杜绝跨用途复用签名。

### 2.2 初始化信任根

```bash
curl -s -X POST http://127.0.0.1:8000/root/init \
  -H 'Content-Type: application/json' \
  -d '{
    "root_version": 1,
    "threshold": 2,
    "threshold_public_keys": ["<阈值公钥1 hex>", "<阈值公钥2 hex>", "<阈值公钥3 hex>"],
    "signer_public_keys": ["<制品签名公钥1 hex>", "<制品签名公钥2 hex>"]
  }'
```

响应（真实截取；`key_id = SHA-256(公钥原始 32 字节)`）：

```json
{"root_version":1,"threshold":2,
 "threshold_keys":{"<key_id>":"<pub hex>", "...": "..."},
 "signer_keys":{"<key_id>":"<pub hex>", "...": "..."}}
```

### 2.3 登记签名制品

```bash
curl -s -X POST http://127.0.0.1:8000/artifacts/sign \
  -H 'Content-Type: application/json' \
  -d '{
    "artifact_type": "firmware",
    "version": "1.0.0",
    "digest": "79f05cea8c57fbfdaad3b08987bebb22c797bf4647c53ce0bf3fcbf75906ea06",
    "key_id": "631f0da0fdd94c325facedcbc5b64e4725d26dabb0866385f0c9d9256b6fad1b",
    "nonce": "099eda030076f19bf23963174e2ec633",
    "signature": "0e17b5cb....3800a"
  }'
```

### 2.4 验签

已登记制品模式（按 `类型+版本` 取登记签名，对调用方给出的当前摘要验签）：

```bash
curl -s -X POST http://127.0.0.1:8000/artifacts/verify \
  -H 'Content-Type: application/json' \
  -d '{"artifact_type":"firmware","version":"1.0.0",
       "digest":"<当前磁盘上制品正文的 SHA-256>",
       "nonce":"<登记时的 nonce>","signature":"<登记时的签名>"}'
```

无状态模式（调用方自带公钥，额外给 `public_key`，可选给 `key_id` 做一致性校验）：

```bash
curl -s -X POST http://127.0.0.1:8000/artifacts/verify \
  -H 'Content-Type: application/json' \
  -d '{"artifact_type":"firmware","version":"1.0.0","digest":"...",
       "nonce":"...","signature":"...","public_key":"<公钥 hex>"}'
```

成功：`{"valid":true,"reason":"验签通过",...,"signer_trusted":true}`；
正文被篡改：`{"valid":false,"reason":"登记签名与当前摘要不匹配（正文已被篡改）",...}`；
轮换后旧密钥签的旧制品：`"valid":false,"signer_trusted":false`。

### 2.5 信任根轮换

请求体描述**新根的完整内容**，并携带旧根阈值成员的批准签名（对同一新根内容逐个签名）：

```bash
curl -s -X POST http://127.0.0.1:8000/root/rotate \
  -H 'Content-Type: application/json' \
  -d '{
    "new_root_version": 2,
    "new_threshold": 2,
    "new_threshold_public_keys": ["<新阈值公钥1>", "<新阈值公钥2>"],
    "new_signer_public_keys": ["<新制品签名公钥>"],
    "approvals": [
      {"key_id": "<旧根成员1 key_id>", "signature": "<对新根内容的 Ed25519 签名>"},
      {"key_id": "<旧根成员2 key_id>", "signature": "<对新根内容的 Ed25519 签名>"}
    ]
  }'
```

批准签名用 `crypto.sign_root_rotation(...)` 生成（见 `examples/curl_walkthrough.sh`）。
批准人不属于当前旧根、批准签名对不上被改的新根内容、人数不足 threshold，一律 `403`。

---

## 3. 自动化测试

```bash
. .venv/bin/activate
python -m pytest -q
```

### 实测结果（2026-09-24，本机）

```
28 passed, 1 warning in 0.43s
```

覆盖（`tests/test_crypto.py` 9 个、`tests/test_api.py` 19 个）：

- **正文篡改**：登记后换摘要验签 `valid=false`；篡改载荷直接登记 `400`；
- **跨类型复用**：A 类型签名声明为 B 类型，登记 `400`、验签 `valid=false`；
  另测制品/根轮换两套域分离前缀互不兼容；
- **重复签名**：同一载荷重复登记 `409`；换内容换类型但复用旧 nonce `409`；
- **失效根/密钥**：非阈值成员批准轮换 `403`；轮换后旧签名者登记 `403`、
  旧制品验签 `valid=false & signer_trusted=false`；
- **轮换阈值**：1/2 批准 `403`、2/2 成功、同一成员重复批准 `400`、
  批准签名与被篡改的新根内容不匹配 `403`、根版本回退 `409`；
- **版本回退**：每类型独立的严格递增检查（相等也拒绝）；
- 非法 hex / 非法版本号 `400`；`STATE_PATH` 持久化后“重启”状态与回退保护仍在。

### 端到端实测

```bash
STATE_PATH=data/demo-state.json uvicorn app.main:app --port 8000 &
python examples/demo.py                 # 程序化 HTTP 演示：19/19 检查通过
bash examples/curl_walkthrough.sh       # 纯 curl 走查 9 个场景
```

`python examples/demo.py` 实测末尾输出：`结果：19 通过，0 失败`。
curl 走查实测（节选）：

```
### 3) 验签真实摘要  -> {"valid":true,...,"signer_trusted":true}
### 4) 正文被篡改    -> {"valid":false,"reason":"登记签名与当前摘要不匹配（正文已被篡改）"}
### 5) 重复登记      -> HTTP 409 拒绝版本回退/重复登记...
### 6) 版本回退      -> HTTP 409 ...已见最高版本 2.0.0，收到 1.5.0...
### 7) 1/2 批准轮换  -> HTTP 403 批准不足：需要至少 2 个旧根成员，实际有效 1 个
### 8) 2/2 批准轮换  -> 200，root_version=2，成员全部换成新公钥
### 9) 旧签名者登记  -> HTTP 403 ...不在当前信任根的授权签名者中
```

> 每次运行会重新生成随机密钥/nonce，所以输出中的 hex 不同，但状态码与判定一致。

---

## 4. 目录结构与安全说明

```
app/
  crypto.py    # Ed25519 签名、规范化消息编码、key_id、版本解析
  store.py     # 内存态 + JSON 原子持久化（RLock）
  schemas.py   # Pydantic 模型
  service.py   # 根初始化/轮换、登记防重放防回退、验签（业务规则）
  main.py      # FastAPI 路由
tests/         # pytest（密钥全部在用例内临时生成）
scripts/gen_test_keys.py
examples/demo.py            # httpx 端到端演示
examples/curl_walkthrough.sh# curl 走查（请求体用内嵌 Python 现签名生成）
requirements.in / requirements.lock
```

**已知边界（教学实现，勿直接上生产）：**

- 单实例 + 本地 JSON 存储，无多实例并发协调、无数据库；
- 无认证/鉴权/RBAC、无 TLS、无审计日志、无速率限制；
- nonce 集合随状态无限增长，生产上应配合有界时间窗 + 外部存储；
- 根/制品没有有效期（expiry）与时间戳，也不做 TUF 式快照/目标角色委托；
- 轮换后旧制品即判为“当前根不信任”，未实现“宽限期/时间钉”策略（代码中有
  `registered_at_root_version` 字段，可据此扩展按登记时点根判定）；
- 验签只接收摘要而非完整正文，服务无法独立证明摘要确实来自某份正文（调用方需自行对正文求 SHA-256）。

## 5. 未完成项

- 无（本次验收要求的功能、测试、锁定依赖、README、实测均已完成）。
  上述「已知边界」属于刻意的范围裁剪，不是验收项的缺漏。
