# 审计日志防篡改证明（Tamper-Evident Audit Log）

纯后端服务：以**规范化记录 + 哈希链 + Ed25519 签名检查点**实现可验证的追加式审计日志，
支持区间导出与独立验证。**信任锚（签名公钥、可信检查点）由验证者自行持有**，
服务端不替验证者保存锚。

## 原理

- **规范化**：所有进入哈希/签名的对象先序列化为规范化 JSON
  （键排序、无空白、UTF-8、不转义非 ASCII），同一逻辑对象得到唯一字节串（`app/canonical.py`）。
- **哈希链**：每条记录 `entry = {seq, ts, actor, action, payload, prev_hash}`，
  `entry_hash = sha256(canonical(entry))`，`prev_hash` 指向前一条的 `entry_hash`
  （创世记录为 64 个 0）。删改、中间删除、重排都会使链条断裂。
- **签名检查点**：定期检查点 `{log_id, upto_seq, head_hash, key_id, created_at}`
  用服务端 Ed25519 私钥签名。验证者把某次检查点（连同公钥）固定为**可信锚**，
  之后任何时刻都可用它判定日志是否被截断或改写。

### 判定语义（`POST /verify` 的 `verdict`）

| verdict | 含义 |
|---|---|
| `VALID` | 链完整，链头与最新可信锚一致 |
| `VALID_UP_TO_ANCHOR` | 链在锚之前的部分已证实；**锚之后的尾部无锚覆盖，被截断/改写不可判定** |
| `TAMPERED` | 删改 / 重排 / 中间删除导致哈希链断裂，或链与锚矛盾 |
| `TRUNCATED` | 链自身完整，但有效签名的检查点（自带或可信锚）证明头上还有更多记录 —— **可判定的截尾** |
| `FORGED_CHECKPOINT` | 检查点签名校验失败，或签名有效但内容与链不符 |
| `CHAIN_VALID_NO_ANCHOR` | 链内部自洽但无锚可用，整体真实性不可判定 |

**可判定与不可判定的截尾**（验收要点）：

- 截掉**可信锚覆盖范围内**的记录 → 锚里的 `upto_seq`/`head_hash` 与导出不符，`TRUNCATED`，可判定。
- 截掉**最新可信锚之后**追加的记录 → 剩余链头恰好等于锚，形式上是 `VALID`，
  验证者**无法发现**。服务在 `detail` 与 `verified_up_to_seq` 中如实报告验证上限，
  不会声称超出锚覆盖范围的绝对可信。要重新获得截尾可判定性，必须定期创建新检查点
  并由验证者固定为新锚。

## 目录结构

```
app/
  canonical.py   规范化 JSON 与哈希
  chain.py       记录与检查点结构
  keys.py        Ed25519 密钥加载/生成
  storage.py     追加式 JSONL 存储
  verify.py      验证逻辑（验证者视角）
  main.py        FastAPI 应用与 HTTP 接口
tests/test_audit.py  15 个验收测试
scripts/demo.sh      端到端 curl 演示
requirements.txt     锁定依赖
```

## 安装与启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 启动（首次启动自动生成 Ed25519 密钥对到 keys/，数据写入 data/）
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000

# 可选环境变量：AUDIT_DATA_DIR（默认 data/）、AUDIT_KEY_DIR（默认 keys/）
```

运行测试：

```bash
.venv/bin/python -m pytest tests/ -v
```

端到端演示（服务运行中，另开终端，需要 `jq`）：

```bash
./scripts/demo.sh
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/public_key` | 签名公钥（**仅演示用**；生产中验证者应带外获取并固定） |
| POST | `/entries` | 追加记录，body：`{"actor","action","payload"}` |
| GET | `/entries?from_seq=&to_seq=` | 查询区间 |
| POST | `/checkpoints` | 对当前链头创建签名检查点 |
| GET | `/checkpoints` | 列出全部检查点 |
| GET | `/export?from_seq=&to_seq=` | 区间导出：记录 + 最新签名检查点 |
| POST | `/verify` | 验证导出；验证者在 body 中自带公钥与可信锚 |

### 请求样例

追加记录：

```bash
curl -X POST http://127.0.0.1:8000/entries \
  -H 'Content-Type: application/json' \
  -d '{"actor":"alice","action":"invoice.approve","payload":{"invoice_id":"INV-101"}}'
```

创建检查点（验证者保存响应作为可信锚）：

```bash
curl -X POST http://127.0.0.1:8000/checkpoints
# => {"checkpoint": {"log_id":"audit-log","upto_seq":5,"head_hash":"sha256:...",
#                    "key_id":"...","created_at":"..."},
#     "signature": "<base64url>"}
```

导出并验证（验证者自带信任锚）：

```bash
curl http://127.0.0.1:8000/export > export.json

curl -X POST http://127.0.0.1:8000/verify \
  -H 'Content-Type: application/json' \
  -d '{
    "entries": <export.json 中的 entries>,
    "public_key_pem": "<验证者持有的公钥 PEM>",
    "checkpoint": <export.json 中的 checkpoint>,
    "checkpoint_signature": "<export.json 中的 checkpoint_signature>",
    "trusted_checkpoint": <验证者此前固定的检查点>,
    "trusted_signature": "<对应签名>"
  }'
```

## 威胁模型与边界

- 能发现：单条删改、中间删除、重排、中间截断、锚覆盖范围内的截尾、伪造/篡改检查点。
- 不能发现（如实声明）：**最新可信锚之后**的尾部截断或改写 —— 必须靠定期新检查点缩小盲区；
  服务端私钥泄露则攻击者可伪造检查点（私钥保护不在本项目范围）；
  验证者若不固定锚而每次都信服务端当前状态，则无任何保证。
- 存储为本地 JSONL 文件，仅追加；未做分布式复制、未做时间戳权威源（ts 为服务端本地时间）。

## 实测结果

- `pytest`：15 个测试全部通过（详见下方"交付说明"提交记录）。
- `scripts/demo.sh`：已对运行中的服务实际执行，VALID / TAMPERED / TRUNCATED 三种判定均如预期。

未完成项：无界面（按要求）；未实现检查点自动定期创建（需手动 `POST /checkpoints`
或外部 cron 触发）；未做公钥带外分发的具体机制（README 仅约定要求）。
