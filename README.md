# 权益惩罚证据归并后端（Validator Equivocation Slasher）

纯后端服务（无前端页面）：接收带**真实 Ed25519 测试签名**的离线投票，归并验证者
**双签（equivocation）证据** 并执行**幂等权益惩罚**。技术栈：Python 3.12 + FastAPI +
PostgreSQL 16（psycopg v3）。

## 核心语义

| 规则 | 实现 |
|---|---|
| 双签识别 | 按 `(chain_id, validator_pubkey, round)`：同一验证者同一轮对**两个不同区块哈希**投票 |
| 相同投票 | 内容哈希（签名覆盖字节的 SHA-256）相同 → `duplicate`，**不构成双签** |
| 签名错误 | 真实 Ed25519 验签失败 → `422 bad_signature` |
| 跨链混用 | URL 与载荷 chain_id 不一致 → `400 chain_mismatch`；公钥未在该链注册 → `422 validator_not_registered`；chain_id 绑定在被签字节内，签名无法跨链重放 |
| 证据唯一 ID | 两份冲突票的「签名覆盖字节」**字典序排序**后拼接做 SHA-256 → 逆序到达得到同一 ID |
| 迟到/重复 | 证据已存在时新票只归档（`already_evidence`）；`UNIQUE(chain_id, validator, round)` + 行级事务锁保证**只处罚一次** |
| epoch 冻结 | `POST .../epochs/{epoch}/freeze` 把当前权益固化为**不可变快照**；惩罚引用快照基数，后续后委托/解绑只改当前权益表，改不了历史基数 |
| 快照未就绪 | 证据保持 `pending`，**绝不**用当前权益凑数；冻结后自动恢复补罚 |
| 崩溃恢复 | 证据先于惩罚提交；`POST /api/v1/admin/recover` 幂等补罚所有 pending 证据 |
| 证据留存 | 投票原文（`raw_vote` JSONB）、证据原文（`raw_evidence`）、规范化字节（`canonical_blob`）与**判定版本**（`judge_version = equivocation-rules/v1.0.0`）全部落库 |

惩罚比例：`1/100`（1%），整数运算：`slashed = base_power * 1 // 100`。

## 目录结构

```
app/
  main.py        FastAPI 路由与错误映射
  slashing.py    归并/惩罚/快照/恢复核心逻辑
  crypto.py      Ed25519 验签、规范化编码、证据/快照哈希
  schema.sql     建表 DDL（唯一约束即正确性保证）
  config.py      判定版本、惩罚比例、故障注入开关
  db.py          psycopg 连接池
scripts/
  gen_examples.py  生成确定性示例密钥与签名投票
  demo.py          端到端演示（建链→冻结→双签→处罚→恢复）
tests/           31 个自动化测试（含真实进程崩溃恢复）
examples/        生成的示例输入（密钥 + 投票 JSON）
```

## 本地启动

```bash
# 1. 准备数据库（本机 PostgreSQL）
sudo -u postgres psql -c "CREATE ROLE slasher LOGIN PASSWORD 'slasher_pw' CREATEDB;"
sudo -u postgres createdb -O slasher slasher_db
sudo -u postgres createdb -O slasher slasher_test   # 测试库

# 2. 安装依赖（锁定版本见 requirements.lock）
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 3. 生成示例输入（确定性密钥，可复现）
python scripts/gen_examples.py

# 4. 启动服务（首次启动自动建表）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

数据库连接通过环境变量覆盖：
`DATABASE_URL=postgresql://slasher:slasher_pw@localhost:5432/slasher_db`

## 验收命令

```bash
# 全量自动化测试（31 个，含并发归并与真实进程崩溃恢复）
. .venv/bin/activate
python -m pytest

# 端到端演示（需服务已在 8000 端口运行）
python scripts/demo.py

# 手工验收示例
curl -s localhost:8000/health
curl -s -X POST localhost:8000/api/v1/chains -H 'content-type: application/json' \
     -d @examples/chain.json
# 注册验证者（公钥见 examples/keys.json）
ALICE_PUB=$(python3 -c "import json;print(json.load(open('examples/keys.json'))['validators']['alice']['pubkey_hex'])")
curl -s -X POST localhost:8000/api/v1/chains/test-chain-1/validators \
     -H 'content-type: application/json' \
     -d "{\"validator_pubkey\": \"$ALICE_PUB\", \"moniker\": \"alice\"}"
curl -s -X PUT localhost:8000/api/v1/chains/test-chain-1/validators/$ALICE_PUB/power \
     -H 'content-type: application/json' -d '{"power": 100000000}'
# epoch 0 边界冻结
curl -s -X POST localhost:8000/api/v1/chains/test-chain-1/epochs/0/freeze
# round 7 双签：两张不同区块的票
curl -s -X POST localhost:8000/api/v1/chains/test-chain-1/votes \
     -H 'content-type: application/json' -d @examples/vote_round7_blockA.json
curl -s -X POST localhost:8000/api/v1/chains/test-chain-1/votes \
     -H 'content-type: application/json' -d @examples/vote_round7_blockB.json
# 查询证据（含原文、判定版本、惩罚引用的快照）
curl -s localhost:8000/api/v1/evidences
# 崩溃恢复入口（幂等）
curl -s -X POST localhost:8000/api/v1/admin/recover
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 + 判定版本 |
| POST | `/api/v1/chains` | 建链（`chain_id`, `epoch_length`） |
| POST | `/api/v1/chains/{cid}/validators` | 注册验证者公钥 |
| PUT | `/api/v1/chains/{cid}/validators/{pub}/power` | 设置当前权益（委托/解绑） |
| GET | `/api/v1/chains/{cid}/validators` | 验证者列表 |
| POST | `/api/v1/chains/{cid}/epochs/{epoch}/freeze` | epoch 边界冻结快照（并触发恢复） |
| GET | `/api/v1/chains/{cid}/epochs/{epoch}/snapshot` | 查询快照 |
| POST | `/api/v1/chains/{cid}/votes` | 提交离线投票（归并入口） |
| GET | `/api/v1/evidences` / `/api/v1/evidences/{id}` | 证据列表 / 详情（原文+惩罚） |
| POST | `/api/v1/admin/recover` | 崩溃恢复：补罚 pending 证据 |
| GET | `/api/v1/params` | 判定版本、惩罚比例、签名方案 |

投票提交返回 `result`：`first`（首票）/ `duplicate`（相同票）/
`new_evidence`（归并出新证据并处罚）/ `already_evidence`（迟到票，只归档）。

## 测试覆盖（tests/）

- `test_crypto.py` — 真实 Ed25519 往返/篡改拒绝、chain_id 绑定、证据排序规范化
- `test_api.py` — 签名错误、跨链混用（URL/载荷不一致、未注册链、公钥跨链复用）、
  相同票非双签、**双签逆序到达产生相同证据 ID**、迟到票不二次处罚
- `test_epoch.py` — **边界轮次**（round 9→epoch 0、round 10→epoch 1）、
  快照未冻结时处罚挂起、后续委托不改变历史惩罚基数、重复冻结拒绝
- `test_concurrency.py` — **并发归并**：冲突票并发只产生一份证据一条惩罚、
  9 轮重复压测、相同票并发只落一张
- `test_crash_recovery.py` — **真实进程崩溃**：`SLASHER_CRASH_AFTER_EVIDENCE=1`
  启动的 uvicorn 子进程在「证据已提交、惩罚未应用」时硬退出（exit 27），
  重启后恢复接口恰好补罚一次；惩罚函数幂等性

## 设计说明

- **计算、协议与密码操作均为真实执行**：Ed25519 验签用 `cryptography` 库，
  证据 ID / 内容哈希 / 快照哈希为真实 SHA-256，惩罚为整数运算，无任何模拟。
- **正确性由数据库约束兜底**：`votes` 的内容唯一索引、`evidences` 的冲突键唯一约束、
  `penalties` 的证据主键，配合归并事务内对验证者行的 `SELECT ... FOR UPDATE`，
  并发下也不会双罚。
- **两段式提交**：投票+证据在一个事务提交后才应用惩罚（独立事务）。
  崩溃只会留下 `pending` 证据，恢复接口幂等补罚，绝不重复处罚。
- 故障注入开关 `SLASHER_CRASH_AFTER_EVIDENCE=1` 仅用于崩溃恢复测试。
