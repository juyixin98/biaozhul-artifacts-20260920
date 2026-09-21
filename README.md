# VaultCommand — 离线 EVM 交易签名后端

FastAPI + SQLAlchemy 2.0 + PostgreSQL 实现的**离线** EVM 交易签名服务。
本地测试用途：只管理测试私钥和待签名交易，**不连接任何链、不广播交易、不接行情**。

## 能力一览

- **钱包**：软件托管（custodial）与仅观察（watch-only）两类。
  - 托管私钥用本地配置的 **AES-256-GCM 主密钥**加密落库（`nonce(12) || ciphertext+tag`）。
  - 明文私钥只存在于签名瞬间的局部变量里；响应、日志、异常、审计中均不出现。
  - 仅观察钱包没有私钥、**不能签名**（调用返回 403）。
  - 所有资源按用户隔离；访问他人资源统一返回 404，不泄露存在性。
- **草稿（draft）**：含 `chain_id / to / value(wei) / gas / gas_price(wei) / nonce / data`；
  提交签名后草稿**冻结**，内容不可改、不可删。
- **签名**：使用成熟库 [`eth-account`](https://github.com/ethereum/eth-account)
  / `eth-keys` 生成 **EIP-155 兼容的 secp256k1 签名**（RLP legacy 交易），
  无任何自写密码学、无假签名。任何人都可以**离线**验证：
  `Account.recover_transaction(raw_hex) == 钱包地址`。
- **幂等**：每个签名请求带 `idempotency_key`（按用户唯一）。同键不同内容 → 409 冲突；
  同键重试 → 返回同一个结果。
- **nonce 排他**：同一 `钱包 + chain_id + nonce` 在有 pending/success 占用时，
  不能签出**不同内容**的交易（数据库部分唯一索引兜底）。相同内容视为重放，返回原结果。
- **并发额度与冷却**：以 wei 计的每日额度 + 签名冷却期。
  额度的**检查与占用在单个数据库事务内原子完成**（钱包行 `SELECT … FOR UPDATE` 串行化），
  并发签名不可能双花额度。
- **状态机**：`pending → success | failed | released`，全部持久化可查询。
- **审计**：只记录交易**摘要**（chain/nonce/to/value/tx_hash）与操作结果，不存任何明文密钥。

## 目录结构

```
app/
  config.py          # 环境配置（VAULT_* 环境变量）
  db.py models.py    # SQLAlchemy 引擎与模型
  crypto.py          # AES-256-GCM 主密钥加解密
  signing.py         # eth-account 离线签名 + 离线解码/恢复
  services.py        # 核心编排：幂等/nonce/额度/冷却/两阶段提交/恢复
  security.py        # Bearer API Key（仅存 SHA-256 摘要）
  audit.py           # 审计落库（只存摘要）
  routers/           # /users /wallets /drafts /sign-requests /audit-logs
alembic/             # 迁移（含部分唯一索引）
scripts/             # 入口、测试密钥生成、挂起恢复、建用户
tests/               # 37 个测试（真 PostgreSQL、真并发线程）
docker-compose.yml   # 一键起 PostgreSQL + API
```

## 快速开始（Docker）

```bash
docker compose up --build
# API:   http://localhost:38080  文档: /docs (宿主端口可在 docker-compose.yml 改)
# PostgreSQL: localhost:5544
```

compose 已内置本地测试主密钥（32 字节全 0 的 base64）。**生产请换密钥**：

```bash
python3 -c 'import base64,os;print(base64.b64encode(os.urandom(32)).decode())'
```

设到环境变量 `VAULT_MASTER_KEY_B64`。`VAULT_ENVIRONMENT=production` 时若未配置主密钥，
服务拒绝启动。

## 本地开发（不用 Docker 跑 API）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
# 需要一个 PostgreSQL（compose 里的 db 即可，或自行起一个）
export VAULT_DATABASE_URL='postgresql+psycopg2://vault:vault@localhost:5544/vault'
alembic upgrade head
uvicorn app.main:app --reload --port 38080
```

## 一次完整调用

```bash
# 1) 注册（本地开发开放自助注册），API Key 只显示这一次
API_KEY=$(curl -s -X POST localhost:38080/users -H 'Content-Type: application/json' \
  -d '{"name":"alice"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["api_key"])')

# 2) 导入一个测试私钥创建托管钱包（地址由服务端派生）
WALLET=$(curl -s -X POST localhost:38080/wallets -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d '{
    "label":"hot", "kind":"custodial",
    "private_key_hex":"0x079e6e68f056bcd04f10d3ec57ffc8206140e2c43e665aa1464300ad5dc23116"}')
WALLET_ID=$(echo "$WALLET" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 3) 建草稿
DRAFT=$(curl -s -X POST localhost:38080/drafts -H "Authorization: Bearer $API_KEY" \
  -H 'Content-Type: application/json' -d "{
    \"wallet_id\":\"$WALLET_ID\",\"chain_id\":31337,
    \"to_address\":\"0x30BB604CCC63a0c8B0E50f7d9117bDA6AEBAA5A8\",
    \"value_wei\":12345678,\"gas\":21000,\"gas_price_wei\":20000000000,
    \"nonce\":5,\"data_hex\":\"0x\"}")
DRAFT_ID=$(echo "$DRAFT" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 4) 提交签名（幂等键）。响应丢失就用同一个键再 POST 一次，得到同一结果。
curl -s -X POST "localhost:38080/sign-requests/$DRAFT_ID/submit" \
  -H "Authorization: Bearer $API_KEY" -H 'Content-Type: application/json' \
  -d '{"idempotency_key":"order-123-v1"}'
```

成功响应包含 `raw_transaction_hex`（可离线验证/以后自行广播）和 `tx_hash`
（= keccak256(raw)）。

### 离线验证（不依赖链、不依赖本服务）

```python
from eth_account import Account
Account.recover_transaction(raw_hex)   # 应等于托管钱包地址
```

测试里另有一份**完全独立**的验证（手写 EIP-155 签名哈希 + `eth_keys` 原始公钥恢复
+ RLP 解字段逐字段比对），见 `tests/test_verify_and_audit.py`。

## API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/users` | 本地注册，返回一次性 API Key（可用 `VAULT_ALLOW_REGISTRATION=false` 关闭） |
| POST/GET | `/wallets`、`GET /wallets/{id}` | 托管/仅观察钱包 |
| POST/GET/PATCH/DELETE | `/drafts[/{id}]` | 草稿；冻结后不可改删 |
| POST | `/sign-requests/{draft_id}/submit` | 提交签名，body `{"idempotency_key": ...}` |
| GET | `/sign-requests[/{id}]` | 查询状态/结果（pending/success/failed/released） |
| POST | `/sign-requests/{id}/resume` | 恢复崩溃后仍 pending 的请求（同一占用行） |
| POST | `/sign-requests/{id}/release` | 放弃 pending 占用，**归还额度与 nonce** |
| GET | `/audit-logs` | 当前用户的审计轨迹（只含摘要） |

所有鉴权接口要求 `Authorization: Bearer <api-key>`。

## 额度、冷却与崩溃恢复的规定（重点）

每个签名请求分两阶段提交：

1. **阶段一（一个事务，原子）**：锁钱包行 → 查当日（UTC）占用、冷却、nonce 占用
   → 插入 `pending` 行（占用 `value_wei`、占住 nonce）并冻结草稿 → **提交**。
2. **阶段二（本地 CPU 签名）**：在**同一行**上写 `success`（签名结果）或
   `failed`（并把 `quota_day` 置空、释放 nonce）→ 提交。

由此得到的保证：

- **阶段一提交前崩溃/回滚**：占用从未落库 → **不扣额度**，可原样重试。
- **两阶段之间进程被杀死**：留下一个持久 `pending`，额度与 nonce 仍被它持有
  （所以不会被重复签出）。恢复方式二选一：
  - **resume（续做）**：用原幂等键再 `submit`，或调 `/resume`；
    在同一占用行上补签名 → 同一份结果、**不二次扣费、不产生第二份签名**。
  - **release（释放）**：确认不要这笔时调 `/release`；行置 `released`、
    `quota_day=NULL`，当日额度与该 nonce 立即恢复，可重新提交。
- **成功响应丢失**（阶段二已提交）：用**同一幂等键**重试，返回原 `tx_hash/raw`，
  不产生第二份结果、不重复扣费。
- **签名失败**（如密钥无法解密）：行置 `failed` 并原子释放占用；可在修好后新建请求。
- 运维批量排查/恢复挂起请求：`python scripts/recover_pending.py list|resume|release <id>`。

> 设计取舍说明：`pending` 行有意被持久化（而不是只活在内存事务里），
> 这样“待处理状态”是可观测、可恢复的，且恢复语义明确；签名是确定性的
> （固定私钥+固定交易），续做不会产生另一份不同结果。

## 并发正确性

- 额度检查/占用依赖 **钱包行级锁**（`FOR UPDATE`）+ 同事务读已提交的占用求和，
  高并发下按钱包串行，超额请求拿到 429 且不留下任何占用。
- nonce 排他由数据库 **partial unique index**
  `(wallet_id, chain_id, nonce) WHERE status IN ('pending','success')` 兜底；
  failed/released 不占 nonce。
- 幂等键由 `UNIQUE(user_id, idempotency_key)` 兜底；并发重复键只有一个胜出，
  其余返回同一结果。

## 测试

需要一个可访问的 PostgreSQL（测试会跑 Alembic 迁移，并在每个用例前 TRUNCATE）：

```bash
export VAULT_DATABASE_URL='postgresql+psycopg2://vault:vault@localhost:5544/vaulttest'
export VAULT_MASTER_KEY_B64='AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA='
python3 -m pytest -q
```

37 个测试覆盖：

- **并发额度**：5 线程真实并发，恰好按 wei 容量签出且总额不超；
  并发同 nonce 不同内容只成一个；并发同幂等键只产生一个请求行。
- **nonce 冲突**：同钱包/链/nonce 不同内容 409；相同内容重放原结果；不同链互不影响。
- **幂等重试**：同键同内容同一结果；同键不同内容 409；失败响应不产生占用。
- **崩溃恢复**：阶段一崩溃零占用；两阶段间崩溃留 pending、额度仍被占、可 resume/release；
  成功响应丢失重试不双扣、不重复。
- **跨用户访问**：读/提交/恢复/列举他人资源均被拒（404/空列表）。
- **离线验签**：独立 ECDSA 公钥恢复出托管地址、tx_hash=keccak(raw)、RLP 字段逐一相符。
- 另含：仅观察钱包拒签、草稿冻结、密钥不出现在响应/错误/审计、冷却期、按钱包隔离额度。

### 测试密钥样例

见 `test-keys.sample.json`（由固定且公开的熵生成，**绝不能在真实网络上充值**）。
生成新的一次性测试密钥：

```bash
python3 scripts/generate_test_keys.py -n 3
```

## 安全边界

- 无网络出站：应用代码不调用任何 RPC，签名与验证均离线完成。
- 私钥字段不进日志（SQLAlchemy 引擎日志被调到 WARNING）、不进 API、不进审计、
  422 校验错误也不回显敏感输入。
- API Key 只存 SHA-256 摘要；明文仅在创建时返回一次。
- 本项目面向本地测试，未包含速率限制、TLS 终结、HSM/KMS 托管等生产加固。
