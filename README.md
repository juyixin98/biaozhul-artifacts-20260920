# VaultCommand — 离线 EVM 交易签名后端

FastAPI + SQLAlchemy + PostgreSQL 的离线签名服务。**只处理本地测试密钥与交易：不连接链、不广播、不接行情。**

## 快速启动（Docker）

```bash
cp .env.example .env
# 生成 AES-GCM 主密钥并写入 .env
echo "VAULT_MASTER_KEY=$(openssl rand -base64 32)" >> .env

docker compose up --build
# API: http://localhost:8000  (文档: /docs)
```

容器启动时自动执行 `alembic upgrade head` 完成迁移。

本地开发（SQLite）：

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
export VAULT_MASTER_KEY=$(openssl rand -base64 32)
alembic upgrade head
uvicorn app.main:app --reload
```

## API 概览

所有业务接口需要 `X-API-Key` 头（通过 `POST /users` 获取）。钱包按用户隔离，跨用户访问一律 404。

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/users` | 创建用户，返回 `api_key` |
| POST | `/wallets` | 创建托管（`custodial`）或仅观察（`watch_only`）钱包 |
| GET | `/wallets` | 列出本人钱包 |
| POST | `/drafts` | 创建交易草稿（chain_id、接收地址、整数 wei 金额、gas、nonce） |
| POST | `/drafts/{id}/submit` | 提交并**冻结**草稿内容（生成 content_hash） |
| POST | `/sign` | 签名（请求头 `Idempotency-Key` 必填） |
| GET | `/sign/{id}` / `/sign` | 查询签名请求状态（pending/succeeded/failed） |
| GET | `/audit` | 审计日志（仅交易摘要与操作结果） |

## 安全设计

- **密钥加密**：托管私钥用本地配置的 32 字节 AES-GCM 主密钥（`VAULT_MASTER_KEY`）加密后存储，AAD 绑定钱包 ID。响应、日志、错误、审计中均不出现私钥（密文也不返回）。
- **仅观察钱包**：无密钥，签名请求返回 403。
- **真实签名**：使用 `eth-account`（eth-keys/coincurve，secp256k1 + RFC 6979 确定性签名），可离线用 `Account.recover_transaction` 验签。无任何自写密码算法。
- **草稿冻结**：草稿无任何修改接口；submit 后计算内容哈希，签名只针对冻结内容。

## 幂等与并发语义

- **幂等键**：`(user_id, idempotency_key)` 唯一。同键同内容 → 返回已存结果（成功响应丢失时的恢复手段）；同键不同内容 → 409。
- **nonce 唯一性**：同一钱包 + 链 + nonce 的非失败请求若内容不同 → 409（在钱包行锁 `SELECT ... FOR UPDATE` 下检查，PostgreSQL 上可序列化并发签名者）。
- **每日额度**：按 `(wallet, UTC 日期)` 记账，占用额 = `value + gas_limit × gas_price`（wei）。检查与占用是**单条原子条件 UPDATE**（`used + amount <= limit` 才更新），并发下不会超支。
- **冷却期**：同一钱包两次成功签名之间的最小间隔（`VAULT_COOLDOWN_SECONDS`），违反返回 429。

## 崩溃恢复与额度释放

- 额度占用与 `pending` 签名请求行在**同一事务**提交：崩溃不可能留下"扣了额度却没有可恢复记录"的状态。
- 签名是确定性的（RFC 6979）：进程中断后用同一幂等键重试，会对同一 `pending` 记录**重新执行签名并得到完全相同的签名结果**——不会产生第二份不同结果，也不会重复扣额度。
- **额度释放规则**：仅当请求进入 `failed` 状态时释放当日占用（幂等释放：`quota_released` 标志 + 防负数的条件 UPDATE）。`succeeded` 与 `pending` 不释放。失败请求的 nonce 可以被后续新请求复用。

## 审计

`audit_log` 记录钱包创建、草稿创建/提交、额度占用、签名成功/失败、额度释放，只含交易哈希摘要与操作结果，不含任何密钥材料。

## 测试

```bash
pytest -v
```

覆盖：并发额度原子性、nonce 冲突、幂等重试与同键冲突、崩溃恢复（不双扣、结果唯一）、失败释放额度、冷却期、跨用户隔离、仅观察钱包拒签、离线验签、密钥不泄漏、Alembic 迁移冒烟。

测试密钥样例见 `tests/keys_sample.json`（Hardhat 公开测试账户，**仅限本地测试**）。
