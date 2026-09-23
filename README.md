# IBC 包超时状态模型（教学简化版）

用 **Python + FastAPI + SQLite** 实现的、IBC（Inter-Blockchain Communication）风格的
**数据包生命周期后端**。纯后端，无前端页面。

> **定位声明：这是教学用的简化模型，不是生产级 IBC 实现。**
> 它保留了 IBC 安全模型的骨架——**真实**的 SHA-256 承诺、**真实**的 Ed25519
> 检查点签名、**真实**的 256 层稀疏 Merkle 树存在性/不存在性证明、确认与超时的
> 终态互斥；省略了连接握手的完整子状态、轻客户端的默克尔化共识状态、欺诈证明、
> 费用与治理等。所有计算、协议与密码操作均真实执行，失败如实报错，没有任何模拟占位。

---

## 1. 模型说明

### 1.1 两条（或多条）模拟链与一个中继器

- 每条链有独立的高度、单调时间、Ed25519 签名公钥和一棵状态树（共库但按链隔离）。
- **轻客户端**是信任根：在链 A 上创建指向链 B 的客户端，登记 B 的公钥。
  之后 A 只接受用该公钥验签通过的 B 链检查点。
- **中继器（relayer）**只是搬运工：读状态、取 Merkle 证明、提交交易；
  它不掌握任何链私钥，无法伪造检查点或证明。

### 1.2 通道（绑定顺序模式、对端、版本）

四次握手（跨链步骤必须附对端已签名检查点上的通道状态证明）：

```
ChanOpenInit(A) -> ChanOpenTry(B) -> ChanOpenAck(A) -> ChanOpenConfirm(B)
```

- **顺序模式**：`ORDERED`（必须按序号交付，遇缺口等待）/ `UNORDERED`（乱序逐包去重）；
  TryOpen 时对端模式不一致即拒绝（`ORDERING_MISMATCH`）。
- **版本协商**：两端版本必须一致（`VERSION_MISMATCH`）。
- **对端绑定**：本端通道记录并校验对端通道编号，防止接错通道（`PROOF_KEY_MISMATCH`）。

### 1.3 数据包

每个包携带：源/目的通道、`sequence`、负载、
`timeout_revision_number + timeout_height`、`timeout_time_nanos`（纳秒，0 表示不用）。

源链发包时按 **IBC v1 真实字节规则**写承诺：

```
commitment = sha256(
    u64be(timeout_revision_number) ||
    u64be(timeout_height)          ||
    u64be(timeout_timestamp)       ||
    sha256(data))
```

### 1.4 接收 / 确认 / 超时退款（互斥）

| 阶段 | 目的链 / 源链 | 证明要求 | 结果 |
|---|---|---|---|
| `sendPacket` | 源链 | — | 写承诺到状态树 + 托管资金 |
| `recvPacket` | 目的链 | 源链检查点签名 + 包承诺**存在性证明**（重算承诺逐字节比对） | 写回执 + 确认 |
| `acknowledgePacket` | 源链 | 目的链 ack 证明；无序还要回执证明，有序要 `nextSeqRecv ≥ seq+1` | 落 **ACKED** 终态，删承诺 |
| `timeoutPacket` | 源链 | 无序：回执**不存在性证明**；有序：`nextSeqRecv < seq+1`；或通道 CLOSED 证明 | 落 **TIMED_OUT** 终态，删承诺并退款 |

- **超时边界语义（与 IBC 一致）：目的链高度 `≥ timeout_height` 或时间
  `≥ timeout_time_nanos` 即超时**——`==` 边界就触发。
- **接收与超时互斥**：接收时若已超时直接拒绝（`PACKET_TIMED_OUT`）；
  超时必须证明目的端确实没收到。
- **确认与退款终态互斥**：`ACKED` / `TIMED_OUT` 由单条条件 UPDATE 决定
  （`WHERE status IN ('IN_FLIGHT','DELIVERED')`），SQLite 写事务串行化，
  并发下恰好一个事务落终态，其余得到 `PACKET_ALREADY_FINALIZED`。
- **有序通道缺口**：期望序号未到返回 `SEQUENCE_GAP`（HTTP 409），不写任何状态。
- **无序通道去重**：重复投递幂等返回 `already_received`。
- 有序通道发生超时会连带关闭本端通道（IBC 语义）；关闭通道拒绝收/发包，
  但可用 CLOSED 通道证明完成超时退款。

### 1.5 信任根与检查点（真实密码学）

每个区块（`POST /chains/{id}/commit`）生成一个检查点：

```json
{
  "version": 1, "chain_id": "chainB", "revision_number": 1,
  "height": 7, "time_nanos": 1070000000000,
  "app_hash": "<状态树根，hex32>", "seq": 7
}
```

并用链私钥对 `b"ibc-mini/checkpoint/v1\n" || 规范JSON(上述字段)` 做 **Ed25519
真实签名**。任何跨链证明的验证顺序固定：

1. 检查点存在且字段完整（缺失 → `BAD_PROOF_STRUCTURE`）；
2. `chain_id` 与客户端信任链一致；
3. 用登记的公钥 **验签**（错误 → `INVALID_CHECKPOINT`）；
4. 高度单调不回退、检查点在信任期内（旧检查点 → `STALE_CHECKPOINT`）；
5. 用检查点 `app_hash` 独立重算 Merkle 证明（不读对端库），不符 →
   `PROOF_VERIFICATION_FAILED` / `PROOF_VALUE_MISMATCH`。

### 1.6 崩溃恢复

- SQLite 开启 `WAL + synchronous=FULL`；每次状态机操作一个事务，提交即落盘。
- 服务启动（以及 `GET /integrity`）执行完整性校验：
  1. 对全部已存检查点重新验签；
  2. 用 `smt_kv` 全量重放重建参考状态树，与持久化节点树逐位比对
     （检测半写入/篡改，不一致报 `INTEGRITY_FAILURE` 并拒绝启动）。
- 终态持久化：重启后已 `ACKED`/`TIMED_OUT` 的包不能被二次终结。

---

## 2. 目录结构

```
.
├── app/
│   ├── api.py          # FastAPI 路由
│   ├── engine.py       # 状态机：链/客户端/连接/通道/包/超时/退款/完整性
│   ├── smt.py          # 256 层稀疏 Merkle 树（存在性/不存在性证明）
│   ├── crypto.py       # SHA-256、IBC v1 承诺编码、Ed25519 签名/验签
│   ├── proofs.py       # 检查点新鲜度 + Merkle 证明验证链
│   ├── paths.py        # IBC 风格状态键
│   ├── storage.py      # SQLite 表结构与连接
│   ├── encoding.py     # 十六进制/base64/u64be/规范 JSON
│   ├── errors.py       # 错误码
│   └── relayer.py      # 测试/演示用中继器
├── tests/              # 37 个 pytest 用例
├── examples/           # 示例请求 JSON
├── demo.py             # 不依赖 HTTP 的端到端演示（9 个场景）
├── main.py             # uvicorn 入口
├── requirements.txt    # 直接依赖
└── requirements.lock   # 完整锁定依赖（pip freeze）
```

---

## 3. 本地启动

需要 Python 3.10+（开发环境为 3.12）。

```bash
cd P036/b
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.lock      # 或 pip install -r requirements.txt

# 启动 HTTP 服务（默认 127.0.0.1:8000，库文件 ./ibc_mini.db）
uvicorn app.api:app --host 127.0.0.1 --port 8000
# 或：python main.py
# 自定义数据库：IBC_DB=/tmp/foo.db uvicorn app.api:app
```

交互式文档：<http://127.0.0.1:8000/docs>（Swagger UI）。

---

## 4. 验收命令

```bash
source .venv/bin/activate

# (1) 全部自动化测试：37 passed
python -m pytest

# (2) 端到端演示（真实签名/证明，覆盖 9 个场景）
python demo.py
python demo.py --keep /tmp/ibc_demo.db   # 保留数据库，可重启复验

# (3) 启动真实 HTTP 服务
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

### 4.1 HTTP 手动走一遍（另开终端）

```bash
BASE=http://127.0.0.1:8000

# 建两条链
curl -s $BASE/chains -H 'content-type: application/json' \
  -d @examples/01_create_chain.json
curl -s $BASE/chains -H 'content-type: application/json' \
  -d @examples/02_create_chain_B.json

# 互建轻客户端（信任根 = 对端公钥）与连接
curl -s $BASE/clients -H 'content-type: application/json' -d @examples/03_client_A_trusts_B.json
curl -s $BASE/clients -H 'content-type: application/json' -d @examples/04_client_B_trusts_A.json
curl -s $BASE/connections -H 'content-type: application/json' -d @examples/05_connection.json

# 四次握手：Init 在 A（跨链的 Try/Ack/Confirm 需要状态证明，建议用 demo.py 或 tests 中的 Relayer 编排）
curl -s $BASE/channels/open-init -H 'content-type: application/json' -d @examples/06_chan_open_init.json

# 健康检查 / 完整性
curl -s $BASE/health
curl -s $BASE/integrity
```

> 跨链步骤需要先在对端出块（`POST /chains/{id}/commit`）、再取证明
> （`GET /chains/{id}/proof?key=<hex状态键>`）。完整自动编排见 `app/relayer.py`，
> 它的每个方法都对应真实中继器的一步，`demo.py` 与全部测试都基于它。

### 4.2 HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康 |
| GET | `/integrity` | 检查点验签 + 状态树重建校验 |
| POST | `/chains` | 建链（生成 Ed25519 密钥） |
| GET | `/chains`, `/chains/{id}` | 查询 |
| POST | `/chains/{id}/commit` | 出块：高度+1、时间前进、签名检查点 |
| GET | `/chains/{id}/checkpoint` | 最新检查点 |
| GET | `/chains/{id}/proof?key=<hex>` | 锚定最新检查点的状态证明（未提交状态拒绝出证） |
| POST | `/clients` | 登记信任根（对端公钥），可设 `trusting_period_nanos` |
| POST | `/connections` | 双向连接（要求双向互信客户端） |
| POST | `/channels/open-init` `/open-try` `/open-ack` `/open-confirm` | 四次握手 |
| POST | `/channels/close` | 关闭通道端 |
| GET | `/chains/{id}/channels/{cid}` | 通道状态 |
| POST | `/packets/send` `/recv` `/acknowledge` `/timeout` | 包生命周期 |
| GET | `/chains/{id}/channels/{cid}/packets/{seq}` | 包状态 |
| GET | `/chains/{id}/escrow` | 托管/退款记账 |

错误响应统一为 `{"error": "<CODE>", "message": "..."}`，主要错误码：
`INVALID_CHECKPOINT`、`STALE_CHECKPOINT`、`BAD_PROOF_STRUCTURE`、
`PROOF_VERIFICATION_FAILED`、`PROOF_KEY_MISMATCH`、`PROOF_VALUE_MISMATCH`、
`SEQUENCE_GAP`、`PACKET_TIMED_OUT`、`PACKET_NOT_TIMED_OUT`、
`PACKET_ALREADY_FINALIZED`、`CHANNEL_CLOSED`、`ORDERING_MISMATCH`、
`VERSION_MISMATCH`、`INTEGRITY_FAILURE`。

---

## 5. 测试覆盖（37 个用例）

- `tests/test_crypto_smt.py`（7）：Ed25519 真签名与篡改拒绝、IBC v1 承诺字节编码、
  ack 双 sha256、SMT 存在性/不存在性证明、60 键批量、删除回空根、两树交叉验证。
- `tests/test_channels.py`（6）：四次握手、顺序模式绑定、版本不一致、
  对端编号绑定、缺失/错签名/错链检查点拒绝、状态不匹配拒绝。
- `tests/test_packet_lifecycle.py`（9）：无序正常路径、乱序+去重、
  有序缺口等待与恢复、有序 ack 序列号覆盖、重复 ack 拒绝、超时后 ack 互斥、
  8 线程并发同一 ack 恰好一个成功、ack/timeout 终态竞争唯一。
- `tests/test_timeouts_and_recovery.py`（12）：**高度超时 == 边界两侧**、
  **时间超时 == 边界两侧**、有序超时关通道、旧高度检查点回退拒绝、
  超出信任期检查点拒绝、缺失/错签名检查点在收包路径被拒、
  通道关闭阻断收发但允许 CLOSED 证明超时、源端关闭阻断发包、
  **崩溃重启**状态保留+终态不可二次终结、篡改检查点被启动完整性校验检出。
- `tests/test_http_api.py`（3）：真实 HTTP 全生命周期与 409/400 负例、
  有序缺口 409 后恢复、篡改数据库后服务拒绝启动。

并发测试直接开 8 个真实线程打引擎事务；超时/信任期使用可注入的确定性链时间，
不依赖墙上时钟。

---

## 6. 简化与取舍（诚实清单）

- 连接握手简化为一次性建立双向 OPEN 连接端（未实现 IBC connection 三次握手的
  INIT/TRY/ACK/CONFIRM 子状态与版本协商字段）。
- 共识状态只有 `(height, time, app_hash)`，检查点是中心化的单签名
  （真实 IBC 验证的是链共识组的多签/聚合签名）。
- 没有链上治理、参数变更、手续费、质押与罚没；代币余额只严格跟踪托管池。
- 中继器是库函数而非独立进程；证明生成与验证在同一进程，但验证只使用
  证明自带数据与信任公钥，不读取被验证链的私有状态。
- 状态树为 256 层稀疏 Merkle 树（路径=sha256(key)），教学上足够直观；
  未实现 ICS-23 的存在/不存在证明的标准 protobuf 编码。
