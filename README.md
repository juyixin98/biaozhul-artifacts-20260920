# 资产锁铸守恒核对（离线对账器）

纯后端服务：摄取两条链上的 **锁定（LOCK）/ 铸造（MINT）/ 销毁（BURN）/ 释放（RELEASE）** 事件，
在事件达到各自链的确认高度后入正式账，按跨链协议的关联消息（`message_id`）配对，
输出未配对、重复铸造、无锁定铸造等异常及其**证据路径**（链 / 交易哈希 / 日志序号 / 区块高度）。
服务只报告，**不自动修正链上状态**；每次对账保留完整快照。

技术栈：Python 3.12 · FastAPI · PostgreSQL 16 · SQLAlchemy 2.0 · pytest

## 核心设计

| 设计点 | 实现 |
|---|---|
| 资产标识 | `(source_chain, origin_contract, token_id)` 三元组，两条链上相同合约地址 + tokenID 不会碰撞 |
| 事件去重 | `(chain, tx_hash, log_index)` 数据库唯一约束，重复投递（同批/跨批）不重复计数 |
| 确认高度 | 事件状态机 `PENDING → CONFIRMED`，仅当 `链高度 ≥ 区块高度 + 确认数` 才入正式账（`ledger_entries`） |
| 配对 | 按 `message_id` 分组，组内按 `(block_number, log_index)` 顺序配对 LOCK↔MINT、BURN↔RELEASE |
| 分叉撤销 | `POST /chains/{chain}/reorg`：自某高度起事件标记 `REVERTED`，对应账目同样标记（保留审计轨迹，不删除） |
| 超时未配对 | 锁定/销毁声明的 `dest_chain` 高度超过 `区块高度 + 超时阈值` 仍无对手事件 → `UNMATCHED_LOCK` / `UNMATCHED_BURN` |
| 异常类型 | `UNMATCHED_LOCK`、`UNMATCHED_BURN`、`MINT_WITHOUT_LOCK`、`RELEASE_WITHOUT_BURN`、`DUPLICATE_MINT`、`CONSERVATION_MISMATCH` |
| 守恒校验 | 每资产 `净锁定(locked−released) == 净铸造(minted−burned)`，差额非零即报 `CONSERVATION_MISMATCH` |
| 快照 | 每次 `POST /reconcile` 生成一行 `snapshots`（链高度、各资产守恒视图、异常数），永久保留 |

## 目录结构

```
app/
  config.py       # 配置（数据库连接、默认确认数、配对超时），RECON_* 环境变量覆盖
  db.py           # 引擎与会话
  models.py       # raw_events / ledger_entries / pairs / anomalies / snapshots / chain_heads
  reconciler.py   # 摄取去重、确认推进、配对、异常检测、守恒计算
  main.py         # FastAPI 路由
tests/            # 15 个自动化测试：乱序、超时、分叉、同地址双链、重复投递等
examples/         # 示例输入（正常生命周期 / 异常场景）
scripts/init_db.sh  # 建库建账号
scripts/demo.sh     # 端到端演示
requirements.txt    # 锁定依赖
```

## 本地启动

```bash
# 1. 准备数据库（本机已运行 PostgreSQL；需要 sudo 访问 postgres 超级用户）
bash scripts/init_db.sh        # 创建 recon 账号与 asset_recon / asset_recon_test 两个库

# 2. 安装依赖（建议虚拟环境）
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 3. 配置（可选，默认值即可连本地库）
cp .env.example .env

# 4. 启动服务（首次启动自动建表）
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

## 验收命令

```bash
# 自动化测试（使用独立的 asset_recon_test 库，每个用例清表）
python3 -m pytest tests/ -v

# 端到端演示（服务运行时另开终端执行）
bash scripts/demo.sh
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| POST | `/events/batch` | 批量摄取事件，返回 `ingested` / `duplicates` 计数 |
| PUT | `/chains/{chain}/head` | 设置链高度（可带 `confirmations` 覆盖确认数） |
| GET | `/chains` | 查看各链高度 |
| POST | `/chains/{chain}/reorg` | 分叉撤销：`{"from_height": N}`，自 N（含）起事件与账目作废 |
| POST | `/reconcile` | 执行一次对账并保存快照，可传 `pairing_timeout_blocks` 覆盖超时 |
| GET | `/snapshots` / `/snapshots/{id}` | 快照列表 / 快照详情（含异常与证据） |
| GET | `/anomalies` | 异常查询（可按 `snapshot_id`、`anomaly_type` 过滤） |
| GET | `/ledger` | 正式账查询（可按 `chain`、`status` 过滤） |
| GET | `/assets` | 当前生效账目下各资产守恒视图 |

## 事件格式示例

```json
{
  "chain": "chainA", "tx_hash": "0xlock001", "log_index": 0, "block_number": 100,
  "event_type": "LOCK", "message_id": "msg-0001",
  "source_chain": "chainA", "origin_contract": "0xGoldToken", "token_id": "42",
  "amount": 500, "sender": "0xAlice", "recipient": "0xAliceOnB", "dest_chain": "chainB"
}
```

- `message_id`：跨链协议的关联消息 ID，锁↔铸、销毁↔释放靠它配对；
- `source_chain` / `origin_contract` / `token_id`：资产身份，跨链防碰撞；
- `dest_chain`：LOCK/BURN 声明的对端链，用于超时未配对判定。

## 异常证据示例

```json
{
  "anomaly_type": "DUPLICATE_MINT",
  "message_id": "msg-dup",
  "evidence": [
    {"event_id": 5, "chain": "chainA", "tx_hash": "0xlock100", "log_index": 0, "block_number": 500, "event_type": "LOCK"},
    {"event_id": 6, "chain": "chainB", "tx_hash": "0xmint100a", "log_index": 0, "block_number": 610, "event_type": "MINT"},
    {"event_id": 7, "chain": "chainB", "tx_hash": "0xmint100b", "log_index": 3, "block_number": 611, "event_type": "MINT"}
  ]
}
```
