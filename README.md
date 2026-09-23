# 资产锁铸守恒核对 (Asset Lock/Mint Conservation Reconciler)

双链锁定/铸造桥的**离线对账器**。纯后端：Python 3.12 + FastAPI + PostgreSQL。

它从两条（或多条）链上的 feeder 接收 `LOCK / MINT / BURN / RELEASE` 事件，
按跨链关联消息把「源链锁定 ↔ 目标链铸造」「目标链销毁 ↔ 源链释放」配对，
仅在事件经过各自链的确认深度后计入正式账，幂等去重、可处理分叉撤销，
并输出**未配对、重复铸造、无锁定铸造**等问题的**证据路径**。
**对账器只读链上世界、不自动修正链上状态**，所有结论以快照+证据文件落盘。

---

## 1. 它在核对什么（协议 / 守恒模型）

跨链桥每条跨链消息 `message_id` 必须满足资产守恒：

| 方向 | 源链动作 | 目标链动作 | 守恒关系 |
|---|---|---|---|
| A→B | `LOCK`（锁定原资产） | `MINT`（铸造映射资产） | 同资产、同金额、同 `message_id`，一一对应 |
| B→A | `BURN`（销毁映射资产） | `RELEASE`（释放原资产） | 同资产、同金额、同 `message_id`，一一对应 |

**资产全局标识（防跨链碰撞）** 为三元组：

```
asset_uid = sha256("v1" | source_chain | original_contract | token_id)
```

- `source_chain`：资产原始链；`original_contract`：原始合约地址；
  `token_id`：NFT 编号（同质代币为空串）。
- 两条链上**相同地址**的合约因此是不同资产，杜绝跨链碰撞。
- 金额 `amount` 为非负整数串（最小单位），服务端全程 `int` 精确计算，绝不用浮点。

**配对键**：`(message_id, asset_uid)`。`message_id = sha256(source_chain + ":" + source_nonce)`，
由源链 feeder 对载荷签名背书。

**确认规则**：事件仅在其所在块是**规范链**（可从已登记的当前 tip 沿 `parent_hash` 回溯到）
且 `tip_height - block_height >= confirmation_depth` 时进入正式账（`CONFIRMED`）；
否则为 `PENDING`；曾确认后被分叉甩出规范链则为 `REORGED`。

**幂等**：`(chain_id, tx_hash, log_index)` 为链上事件身份，重复投递不重复计数；
跨链消息维度的重复（如对同一 `LOCK` 铸两次）则计为 **DUPLICATE_MINT**。

---

## 2. 密码学（全部真实执行）

- feeder 对事件载荷用 **Ed25519**（RFC 8032）签名；服务端用登记的 feeder 公钥
  `cryptography.hazmat` **真实验签**，失败拒收（HTTP 400）。
- 规范序列化（RFC 8785 风格）：字典按键排序、紧凑分隔、无多余空白、`ensure_ascii=False`。
- 资产 UID / 消息 ID / 事件去重键 / 快照哈希均为真实 **SHA-256**。
- 每次对账快照计算内容 SHA-256 并**哈希链**接前一次快照（`prev_snapshot_hash`），防篡改可审计。

## 3. 系统只报告、不修复

对账器**不会**触发任何铸造/回滚/释放。所有异常产出为 finding（写库）+
证据文件（JSON，落 `evidence_dir`）+ 快照（落 `snapshot_dir`）。处置由人工/外部系统决定。

---

## 4. 目录结构

```
.
├── app/
│   ├── main.py            # FastAPI 应用与路由
│   ├── config.py          # 环境配置
│   ├── db.py              # 引擎/会话/建表
│   ├── models.py          # SQLAlchemy 表模型
│   ├── schemas.py         # Pydantic API 模型
│   ├── crypto.py          # Ed25519 签名/验签、规范JSON、sha256
│   ├── identity.py        # 资产UID / 消息ID / 事件键 / 金额
│   ├── chains.py          # 链与feeder登记、块/规范链
│   ├── ingest.py          # 事件幂等入库
│   ├── reconcile.py       # 核心对账：确认度、配对、守恒、证据、快照
│   └── demo_crypto.py     # 本地演示用密钥/签名辅助（非生产信任根）
├── scripts/demo.py        # 端到端示例：正常+乱序+超时+分叉+同地址
├── examples/events.json   # 示例输入
├── tests/                 # 自动化测试（真 PostgreSQL）
├── data/                  # 运行期快照与证据（自动创建）
└── requirements.txt
```

---

## 5. 本地启动

### 5.1 准备 PostgreSQL

已有运行在 `localhost:55432`、用户/库 `inbox/inbox` 的 PostgreSQL 16（docker 容器 `inbox-pg`）。
直接使用它，或自建：

```bash
docker run -d --name recon-pg -e POSTGRES_USER=inbox -e POSTGRES_PASSWORD=inbox \
  -e POSTGRES_DB=inbox -p 55432:5432 postgres:16-alpine
```

### 5.2 建虚拟环境并安装锁定依赖

```bash
cd /home/admin/Downloads/biaozhul/P035/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 5.3 配置并启动

```bash
export DATABASE_URL="postgresql+psycopg2://inbox:inbox@localhost:55432/inbox"
# 可选：export ADMIN_TOKEN=change-me   （设置后管理/登记/对账接口需 Bearer）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

首次启动自动建表。OpenAPI 文档：<http://127.0.0.1:8000/docs>。

---

## 6. 验收命令

> 以下命令需要数据库可连，并已 `pip install -r requirements.txt`。

### 6.1 自动化测试（覆盖全部规定场景）

```bash
cd /home/admin/Downloads/biaozhul/P035/b
export TEST_DATABASE_URL="postgresql+psycopg2://inbox:inbox@localhost:55432/postgres"
pytest -v
```

> `TEST_DATABASE_URL` 指向一个**可创建数据库的维护库**（如 `postgres`），
> 测试会自建并反复重建独立库 `recon_acceptance_test`。
> 若不设置，回退到 `DATABASE_URL` 所在服务器的 `postgres` 维护库。

测试覆盖：

1. `test_crypto_real_ed25519_*` — 真实 Ed25519 签名/验签/篡改检测、确定性 UID/消息ID；
2. `test_out_of_order_pairs` — **乱序**：MINT 早于 LOCK 到达，最终正确配对且守恒成立；
3. `test_timeout_unpaired_lock` — **超时未配对**：LOCK 确认后超过 `message_timeout_seconds` 仍无 MINT；
4. `test_reorg_reverts_confirmed_event` — **分叉撤销**：事件先 CONFIRMED，
   更高工作量分叉后变 REORGED，历史快照保留、结论随新快照翻转；
5. `test_same_contract_address_two_chains` — **两链相同地址**不同资产，不互相误配对/不误撞；
6. `test_duplicate_mint` / `test_mint_without_lock` / `test_amount_mismatch` /
   `test_message_asset_mismatch` / `test_conservation_balance` — 各类守恒违例与证据；
7. `test_duplicate_delivery_is_idempotent` — 重复投递不重复计数；
8. `test_snapshots_chained_and_persisted` — 每次对账均保留哈希链快照；
9. `test_api_*` — HTTP 端到端（登记、块、投递、对账、取证据）。

### 6.2 端到端示例脚本（带真实签名、真实落盘）

```bash
cd /home/admin/Downloads/biaozhul/P035/b
export DATABASE_URL="postgresql+psycopg2://inbox:inbox@localhost:55432/inbox"
python -m scripts.demo
```

脚本会：登记两条链与 feeder 公钥 → 用 Ed25519 对事件真实签名 →
演示正常配对、乱序、超时未配对、分叉撤销、两链同地址 → 多次对账 →
打印每次快照路径、哈希链，以及未配对/重复铸造/无锁定铸造的证据文件路径。

加载示例输入文件（`examples/events.json`，内含 11 条真实签名事件）：

```bash
python -m scripts.load_example --reset
```

### 6.3 手工 API 走查

```bash
# 健康检查
curl -s localhost:8000/health

# 登记链（确认深度）与 feeder 公钥；登记块头；再投递已签名事件（见 /docs 与 examples/events.json）
curl -s -X POST localhost:8000/chains -H 'Content-Type: application/json' \
  -d '{"chain_id":"ethereum","name":"Ethereum Sepolia","confirmation_depth":2,"message_timeout_seconds":3600}'
# ……完整报文格式见 examples/events.json 与 scripts/demo.py

# 触发一次对账（离线、不触链）
curl -s -X POST localhost:8000/reconciliations
# 列快照与证据
curl -s "localhost:8000/reconciliations?limit=5"
curl -s localhost:8000/findings?severity=high
```

---

## 7. 关键语义与边界

- **只计确认账**：`PENDING` 事件不参与配对与守恒汇总，但会在快照中单列。
- **分叉**：事件状态在每次对账时按最新规范链重算；`REORGED` 事件不计账，
  并产出 `REORGED_EVENT` finding；事件若在分叉后被重新打包（新 tx/hash）视为新投递。
- **超时**：以事件所在规范块的 `block_time` 判定。无块时间时回退首次见到时间。
- **重复投递**：同 `(chain,tx,log)` 只记一次；不同载荷（equivocation）同身份则拒收并标记。
- **金额守恒**：按 `asset_uid` 汇总 `确认LOCK − 确认MINT` 与 `确认BURN − 确认RELEASE`，
  非零即 `CONSERVATION_IMBALANCE`。
- 所有时间用 UTC；金额用整数字符串传输、`int` 计算。
