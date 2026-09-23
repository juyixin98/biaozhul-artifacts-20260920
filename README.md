# IBC 包超时状态模型（教学后端）

使用 **Python 3.12 + FastAPI + SQLite** 实现的 **IBC 风格包生命周期后端**，纯 API，无前端。
模拟跨链中继场景下数据包的 **发送 → 接收 → 确认（ack）/ 超时退款（timeout）** 状态机，
重点演示 **超时边界、终态互斥、顺序/无序通道、签名检查点信任根与崩溃恢复**。

> ⚠️ **这是明确标注的简化教学模型，不是 ibc-go / cosmos-sdk 的实现。**
> 两条链（chain-a / chain-b）同处一个 SQLite 文件、中继者逻辑在同进程内；
> 真实 IBC 中两条链是独立信任域，消息由外部链下 relayer 跨进程搬运。
> 简化项与偏差见文末「与真实 IBC 的偏差」。

---

## 1. 核心模型

### 1.1 包生命周期（状态机）

```
                 源链 SEND                  目的链 RECV                 源链 ACK
  用户转账 ──► [SENT 托管+承诺] ──relay──► [RECEIVED 写回执] ──relay──► [ACKED 释放托管]
                                  │
                                  └── 超时点到达 + 目的链"未处理"证明 ──► [TIMED_OUT 退款]
```

- 包状态：`SENT → RECEIVED → ACKED`，或在未被目的链处理时 `SENT/RECEIVED → TIMED_OUT`。
- `ACKED` 与 `TIMED_OUT` 是**两个互斥终态**。

### 1.2 终态互斥如何保证（不是应用层 if 判断）

在 `ibc_teach/store.py` 中，ack 与 timeout 的最后一步都是**条件 UPDATE**：

```sql
UPDATE packets SET status='ACKED'      WHERE id=? AND status IN ('SENT','RECEIVED');
UPDATE packets SET status='TIMED_OUT'  WHERE id=? AND status IN ('SENT','RECEIVED');
```

配合每个写事务的 `BEGIN IMMEDIATE`（SQLite 写锁串行化 + busy 重试），
并发 N 个 ack/timeout 下**恰好一个**事务能把包带出非终态，其余 `rowcount=0`，
返回 `409 terminal_state`。该性质由 `tests/test_04_terminal_mutex.py` 的
多线程（20 路 barrier 并发）与白盒直连测试覆盖，且多轮运行稳定。

### 1.3 超时语义（严格 `<`，边界明确）

包携带两个超时条件，任一满足即超时（`0` 表示该条件不启用）：

| 条件 | 判定（在目的链最新已提交块上） | 边界 |
|---|---|---|
| `timeout_height` | `dest_height >= timeout_height` | 相等即超时 |
| `timeout_time_ns` | `dest_time_ns >= timeout_time_ns` | 相等即超时 |

- 边界**内**（`height == timeout-1` / `time == deadline-1ns`）：可接收，见
  `test_height_timeout_boundary_just_before_accepts` 等；
- 边界**点**（`==`）：`RecvPacket` 返回 `408 timeout_*_reached`，
  可凭「未处理」证明做超时退款，返回 `200 final_status=TIMED_OUT`。

### 1.4 顺序通道 vs 无序通道

通道成对创建，**绑定顺序模式（ordered/unordered）、对端链/端口/通道、版本号**。

- **ordered（有序）**：
  - 接收维护 `nextSequenceRecv`，必须 `seq == nextSequenceRecv`；
  - `seq > expected` 返回 `409 sequence_gap`（**遇缺口等待**，状态不变）；
  - ack 也必须按序，以对端 `nextSequenceRecv > seq` 的成员证明证明已收；
  - timeout 以 `nextSequenceRecv <= seq` 证明该序号未被消费。
- **unordered（无序）**：
  - 不要求序号连续，逐包写 `receipt`（固定值 `0x01`，对齐 ICS-004 receipt 占位）；
  - 重复投递返回 `409 already_exists`（**逐包去重**、幂等）；
  - ack 用该序号 receipt 的成员证明；timeout 用该序号 receipt 的**非成员证明**。

### 1.5 信任根：真实 Ed25519 签名检查点 + 稀疏 Merkle 树

所有密码学操作**真实执行**（PyNaCl / libsodium 的 Ed25519，SHA-256），无 mock：

- 每条链创世时生成真实 **Ed25519 共识签名密钥**；每次「出块」
  (`POST /chains/{id}/blocks`) 对规范化负载
  `{chain_id, height, time_ns, app_hash, previous_app_hash}` **真实签名**。
- `app_hash` 是该块提交状态的 **稀疏 Merkle 树（SMT, 256 位键空间）根**：
  - 叶子 `SHA256("leaf-v1"‖key‖value)`，内部节点
    `SHA256("node-v1"‖min‖max)`（域分离 + 内部节点无序化）；
  - 空叶子/空子树根可预计算；插入顺序不影响根（有测试）。
- 跨链消息必须携带对端**已签名检查点 + 状态证明**，目的端依次校验：
  1. 检查点字段齐全；
  2. `verify_key` **必须等于对端创世登记的共识公钥**（错钥/陌生钥拒绝）；
  3. **Ed25519 签名验证通过**（签名缺失/损坏/张冠李戴拒绝）；
  4. 检查点字段与存档块一致（防真签名拼改字段重放）；
  5. 轻客户端该通道信任高度**不得回退**（**旧检查点重放拒绝**）；
  6. **Merkle 成员/非成员证明**对 `app_hash` 验证通过（256 层、层级连续、
     走向位与键一致，任何篡改/长度不足/伪造存在性都拒绝）。

### 1.6 崩溃恢复

- SQLite `WAL + synchronous=FULL`；每个状态转移是一个 `BEGIN IMMEDIATE` 事务，
  提交后才出块，签名检查点与状态快照原子落盘。
- 服务启动 lifespan 执行 `integrity_check()`：**逐块重放**——
  重算每个高度快照的 SMT 根并与 `app_hash` 比对、校验 `previous_app_hash`
  链连续性、用公钥验证每个块签名。任一不符则**拒绝启动**。
- `tests/test_06_crash_recovery.py` 包含**真实 uvicorn 进程 `kill -9` 后重启**
  的端到端用例，以及篡改快照/篡改签名被检测的用例。

---

## 2. 目录结构

```
.
├── ibc_teach/
│   ├── crypto.py      # Ed25519 检查点签名 + 稀疏 Merkle 树（真实密码学）
│   ├── store.py       # SQLite 存储、状态机、终态互斥、证明校验、完整性检查
│   ├── schemas.py     # Pydantic 请求模型与 hex 校验
│   ├── relayer.py     # 中继者辅助：取检查点/证明并组装跨链消息
│   └── app.py         # FastAPI 路由
├── tests/             # 48 个 pytest 用例（含真实进程 kill -9 恢复）
├── examples/
│   ├── demo.py            # 纯标准库端到端演示（4 个场景）
│   ├── walkthrough.sh     # curl 手工走查（超时拒绝 + 退款，带状态断言）
│   └── payloads/          # 示例请求 JSON
├── requirements.in        # 直接依赖（宽松约束）
├── requirements.txt       # 运行时全量锁定（pip freeze）
├── requirements-dev.txt   # 测试额外锁定
└── README.md
```

---

## 3. 本地启动

需要 Python 3.10+（开发实测 3.12）。

```bash
cd /home/admin/Downloads/biaozhul/P036/a

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/pip install -r requirements-dev.txt   # 跑测试需要

# 启动（DB 路径可用环境变量 IBC_TEACH_DB 指定，默认 ./ibc_teach.db）
.venv/bin/uvicorn ibc_teach.app:app --host 127.0.0.1 --port 8000
```

启动时若库是新建的，会自动生成两条链的创世块（高度 0）与 Ed25519 密钥。
交互式 API 文档：<http://127.0.0.1:8000/docs>。

---

## 4. 验收命令（建议按顺序执行）

### 4.1 自动化测试（48 项）

```bash
.venv/bin/python -m pytest tests/ -v
```

覆盖：SMT/签名原语、信任根（缺失/错钥/错签名/旧检查点/伪造成员关系）、
有序缺口与按序确认、无序乱序与去重、**两种超时边界**、通道绑定、
**20 路并发 ack/timeout 的终态互斥**、通道关闭、以及
**真实进程 kill -9 崩溃恢复与篡改检测**。

### 4.2 端到端演示（真实 HTTP + 实时签名/证明）

终端 1：

```bash
IBC_TEACH_DB=/tmp/ibc_demo.db .venv/bin/uvicorn ibc_teach.app:app --port 8000
```

终端 2：

```bash
.venv/bin/python examples/demo.py http://127.0.0.1:8000
```

应看到 4 个场景全部符合预期，末行打印 `DEMO OK`：
无序正常流程、超时点接收被 `408` 并退款 `TIMED_OUT`、
有序 seq2 先到 `409 sequence_gap` 补齐后放行、损坏签名检查点被 `400` 拒绝。

### 4.3 curl 手工走查

```bash
BASE=http://127.0.0.1:8000 PY=./.venv/bin/python bash examples/walkthrough.sh
```

脚本对关键步骤做了 HTTP 状态断言（发送必须 201、超时点接收必须 408、退款必须 200），
任一不符立即非零退出。

### 4.4 快速手工核对

```bash
curl -s localhost:8000/health | .venv/bin/python -m json.tool
curl -s localhost:8000/chains | .venv/bin/python -m json.tool   # 两条链 + 最新检查点
curl -s localhost:8000/admin/integrity | .venv/bin/python -m json.tool
```

---

## 5. HTTP API 摘要

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 |
| GET  | `/chains` | 两条链、验签公钥、最新高度/时间/状态根 |
| POST | `/channels` | 成对创建通道端（ordering/version/对端绑定） |
| POST | `/chains/{chain}/channels/{port}/{channel}/close` | 关闭通道端 |
| GET  | `/chains/{chain}/channels` | 列出通道及 nextSequence* |
| POST | `/chains/{chain}/blocks` | 出块：提交状态、算 SMT 根、Ed25519 签名（body 可空） |
| POST | `/packets/send` | 源链发包（序号、`timeout_height`、`timeout_time_ns`） |
| GET  | `/keys?kind=commitment\|receipt\|next_seq_recv&port&channel[&sequence]` | 语义键 → hex path |
| GET  | `/chains/{chain}/proof?height&path` | 某高度的成员/非成员证明 + 已签名检查点 |
| POST | `/packets/recv` | RecvPacket（检查点+源承诺成员证明） |
| POST | `/packets/ack` | Acknowledgement（receipt 成员 / nextSeqRecv 证明） |
| POST | `/packets/timeout` | Timeout（receipt 非成员 / nextSeqRecv≤seq 证明） |
| GET  | `/packets[?chain_id=]` | 查看包与终态 |
| GET  | `/admin/integrity` | 在线触发块链/状态根/签名完整性重放 |

跨链消息体统一为：

```json
{
  "packet":     { "src_chain": "...", "dst_chain": "...", "sequence": 1,
                  "timeout_height": 10, "timeout_time_ns": 0,
                  "data_hex": "deadbeef", "amount": 100, "...": "..." },
  "checkpoint": { "chain_id": "chain-a", "height": 3, "time_ns": 1700000000000000000,
                  "app_hash": "<64 hex>", "previous_app_hash": "<64 hex>",
                  "signature": "<128 hex>", "verify_key": "<64 hex>" },
  "proof":      { "steps": [ {"level": 0, "bit": 0, "sibling": "<64 hex>"} ],
                  "value_hex": "<仅 ordered 的 nextSeqRecv 证明需要>" }
}
```

典型错误码：`400 proof_verification_failed / bad_request`、
`404 not_found`、`408 timeout_height_reached / timeout_timestamp_reached`、
`409 sequence_gap / already_exists / channel_closed / terminal_state`。

---

## 6. 与真实 IBC 的偏差（教学简化，务必知悉）

1. 两条链与中继者在**同一进程、同一 SQLite 文件**；真实环境是独立进程/独立信任域。
2. 共识被简化为「单一 Ed25519 签名」；真实 Tendermint 为多验证人 + 投票权集，
   且存在误责（misbehaviour）与信任周期（trusting period）。
3. 证明简化为**单一 SMT 状态根 + 256 层路径**；ICS-023 支持多 store、多前缀、
   存在性/不存在性的不同编码格式。
4. 状态键布局是教学命名（`packets/commitments/...` 等），与 ibc-go 的
   exact bytes 布局不完全一致；承诺体是规范化 JSON 而非 ICS-004 的
   `sha256(timeout‖data)` 字节串（语义同构、字段更全）。
5. 没有连接握手（ConnOpen*）、通道握手四步、代币托管的银行模块；
   `amount` 仅作为退款/释放数额的整数记账。
6. 时间戳是各链块内的单调纳秒；没有 BFT 时间不确定性（time uncertainty）。
7. 轻客户端只做「验签 + 高度不回退 + 存档一致性」，不做时间区间过期与罚没。

尽管如此，**签名验签、Merkle 成员/非成员证明、条件更新的终态互斥、
超时边界判定、事务持久化与崩溃完整性检查均为真实执行**，
任何伪造证明或错误检查点都会被拒绝，并有自动化测试固定这些性质。
