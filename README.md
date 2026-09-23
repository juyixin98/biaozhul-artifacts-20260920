# 权益惩罚证据归并（Equity Penalty Evidence Aggregation）

纯后端服务（Python + FastAPI + PostgreSQL）。只接收**带真实测试签名的离线投票**，
按链 / 验证者 / 高度 / 轮次 / 投票类型识别双签，归并为内容寻址的唯一证据，
并在 epoch 边界冻结的权益快照上**精确执行一次** slash 处罚。

> 所有计算、协议与密码操作均为真实执行：Ed25519 验签（`cryptography`）、
> 规范化哈希（SHA-256）、整数 slash 运算、PostgreSQL 事务/行锁/唯一约束。
> 测试中没有任何桩签名或桩数据库。

---

## 1. 协议与判定规则（judgment version `rules-1.0.0`）

### 1.1 离线投票与真实签名

每条投票是一个 JSON 信封，字段：

| 字段 | 说明 |
|---|---|
| `chain_id` | 链标识，**参与签名** |
| `validator_address` | `lower_hex(SHA256(public_key)[0:20])` |
| `height`, `round` | 高度与轮次（非负 64 位整数，均从 0 开始连续） |
| `vote_type` | `prevote` 或 `precommit`（属不同消息，不互为双签） |
| `block_hash` | 32 字节 hex |
| `public_key` | Ed25519 公钥，32 字节 hex |
| `signature` | 对规范化消息的 **真实 Ed25519 签名**，64 字节 hex |

签名内容（ASCII 域分隔 + 规范化 JSON，键排序、无空白）：

```
EPE-OFFLINE-VOTE/v1\n{"block_hash":...,"chain_id":...,"height":...,"round":...,"validator_address":...,"vote_type":...}
```

- `chain_id` 在签名体内 → 把链 A 的投票原样投到链 B **验签必然失败**（密码学层面拒绝跨链混用，而非约定）。
- 篡改任一字段（高度/区块哈希等）而保留旧签名 → 验签失败。
- 公钥哈希与声称地址不一致 → 结构性拒绝。

### 1.2 双签判定

两份投票构成双签，当且仅当：

```
(chain_id, validator_address, height, round, vote_type) 完全相同
且 block_hash 不同
```

- **两份完全相同的投票（同 block_hash）是重复，不是双签** → 返回 `duplicate_vote`，不产生证据、不处罚。
- 第三种及更晚的冲突 block hash 不会新开案件：每个违规组只有一条证据。

### 1.3 证据规范化与唯一 ID

取该违规组中**最早到达的两个不同 block hash** 的投票原文，各自规范化为 JSON 字节，
按字节序排序后拼接，再做 SHA-256：

```
EPE-EVIDENCE/v1\n<record1>\n<record2>   （record 已按字节排序）
SHA256 → evidence_id
```

排序与到达顺序无关，因此**逆序到达产生完全相同的 evidence_id 与 canonical body**。

### 1.4 迟到 / 重复只处罚一次

三层防线：

1. 归并事务内对违规组取 Postgres 事务级咨询锁（`pg_advisory_xact_lock`），并发投递串行化；
2. `votes`、`evidence(chain,validator,height,round,vote_type)`、`penalties(evidence_id UNIQUE)` 唯一约束兜底；
3. 处罚以证据状态机驱动（`DETECTED → PENALIZED`，缺快照时 `AWAITING_SNAPSHOT`），重复调用幂等。

### 1.5 Epoch 边界冻结权益基数

- `epoch = round // ROUNDS_PER_EPOCH`（默认每 10 轮一个 epoch）。边界轮次：round 9 → epoch 0，round 10 → epoch 1。
- 每个 `(chain, validator, epoch)` 的权益快照在 epoch 边界冻结，**不可变**：
  重复提交相同值是空操作，提交不同值返回 `409`。
- 处罚行**复制**冻结基数 `frozen_power` 并引用 `snapshot_id`。之后的委托只能写入未来 epoch 的快照，
  **永远无法改变历史处罚基数**。
- slash 为整数运算：`slashed = floor(frozen_power * SLASH_RATE_PPM / 1_000_000)`，默认 `SLASH_RATE_PPM=10000`（1%）。

### 1.6 证据原文与判定版本

`evidence.raw_evidence`（JSONB）保存两份投票的**原始信封**；同时保存 `canonical_body`、
`judgment_version`（证据与处罚各自冗余记录），支持审计与未来规则版本并存。

### 1.7 崩溃恢复

证据落盘（事务 1）与处罚执行（事务 2）是**两个独立事务**。若在两者之间进程崩溃：

- 证据已提交为 `DETECTED`（或缺快照时 `AWAITING_SNAPSHOT`），处罚不存在；
- 服务启动时自动执行 `recover_pending()` 对账，把所有非终态证据推进到最终状态；
- 也可手动 `POST /recover`；恢复本身幂等，重复执行不产生第二条处罚。

测试通过故障注入 `CRASH_AFTER_EVIDENCE=1` 真实复现该崩溃点（不是 mock）。

---

## 2. HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查，返回判定版本 |
| POST | `/chains` | 注册链 |
| POST | `/validators` | 注册验证者（`{chain_id, public_key, moniker}`，返回派生地址） |
| POST | `/snapshots` | 冻结某 epoch 权益基数（不可变） |
| POST | `/votes` | 提交一条离线投票，完成验签/归并/处罚 |
| GET | `/evidence?chain_id=` | 证据列表 |
| GET | `/evidence/{id}` | 证据详情（原文、规范化字节、处罚与快照引用） |
| GET | `/penalties` | 处罚列表 |
| POST | `/recover` | 手动触发崩溃恢复对账 |

`POST /votes` 的返回 `status`：
`accepted_no_conflict` / `duplicate_vote` / `penalized` / `already_penalized` / `awaiting_snapshot`。
验签失败、跨链、篡改、未注册链/验证者均返回 `422`。

---

## 3. 目录结构

```
app/
  config.py       # 环境变量配置（判定版本、slash 比例、epoch 长度、故障注入）
  domain.py       # 纯领域规则：Ed25519、地址派生、规范化、证据ID、epoch/slash 数学
  testsign.py     # 测试用真实签名工具（真实 Ed25519 密钥，非桩）
  db.py           # 连接池 + 建表 SQL（唯一约束固化"只罚一次"）
  service.py      # 归并/证据/快照冻结/处罚/恢复（事务边界、咨询锁、状态机）
  schemas.py      # Pydantic 请求模型
  main.py         # FastAPI 路由与启动恢复
tests/            # 26 个自动化测试（领域单测 + 端到端 + 并发 + 崩溃恢复）
examples/         # 生成器 + 带真实签名的示例输入（examples/generated/）
scripts/
  live_demo.py    # 对运行中服务的真实 HTTP 验收（含 16 线程并发归并）
  acceptance.sh   # 一键：重置库→起服务→HTTP验收→故障注入→崩溃重启恢复
requirements.txt  # 完整传递依赖锁定（pip freeze）
docker-compose.yml# 可选：一次性 PostgreSQL 16
```

---

## 4. 本地启动

需要 Python 3.12+ 与 PostgreSQL 14+。

### 4.1 准备数据库

任选一种：

```bash
# A) 用 Docker 起一个一次性 PostgreSQL
docker compose up -d db

# B) 使用本机已有 PostgreSQL（下面是本机 peer 认证的示例）
sudo -u postgres psql -c "CREATE ROLE epe LOGIN PASSWORD 'epe_dev_pw';"
sudo -u postgres createdb -O epe epe
sudo -u postgres createdb -O epe epe_test
```

### 4.2 安装依赖（已锁定）

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

### 4.3 启动服务

```bash
export DATABASE_URL="postgresql://epe:epe_dev_pw@localhost:5432/epe"
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

启动时自动建表并执行一次崩溃恢复对账。OpenAPI 文档：`http://127.0.0.1:8000/docs`。

可调环境变量：`JUDGMENT_VERSION`、`SLASH_RATE_PPM`（默认 10000）、
`ROUNDS_PER_EPOCH`（默认 10）、`CRASH_AFTER_EVIDENCE`（仅测试用）。

---

## 5. 验收命令

### 5.1 一键端到端验收（推荐）

重置开发库 → 起服务 → 真实 HTTP 演示（逆序到达、边界轮次、三类非法输入、
16 线程并发归并）→ 故障注入制造"证据已落盘但处罚未执行"的崩溃 →
重启后自动恢复处罚并验证幂等：

```bash
./scripts/acceptance.sh
```

预期结尾输出 `ALL ACCEPTANCE FLOWS PASSED`。

### 5.2 自动化测试

```bash
export DATABASE_URL="postgresql://epe:epe_dev_pw@localhost:5432/epe_test"
.venv/bin/python -m pytest
```

26 个用例，覆盖：真实签名往返、篡改/错链/错密钥拒绝、证据 ID 顺序无关与分组区分、
epoch 边界数学、slash 整数运算、相同投票非双签、逆序同 ID、迟到第三票与重复只罚一次、
边界轮次引用不同快照、prevote/precommit 分离、快照冻结不可改写、缺快照延后处罚、
**并发归并恰好一次**、**处罚前崩溃→重启恢复→恢复幂等**。

### 5.3 手工 curl 快速验证

先生成带真实签名的示例：

```bash
.venv/bin/python -m examples.generate_examples   # 写入 examples/generated/
```

然后（假设服务已运行）：

```bash
curl -s localhost:8000/health
curl -s -X POST localhost:8000/chains -H 'Content-Type: application/json' \
  -d '{"chain_id":"chain-a-1"}'
curl -s -X POST localhost:8000/validators -H 'Content-Type: application/json' \
  -d @examples/generated/00_validator.json
curl -s -X POST localhost:8000/snapshots -H 'Content-Type: application/json' \
  -d @examples/generated/00_snapshot.json

# 双签（逆序也会得到同一 evidence_id）
curl -s -X POST localhost:8000/votes -H 'Content-Type: application/json' \
  -d @examples/generated/10_vote_blockX.json
curl -s -X POST localhost:8000/votes -H 'Content-Type: application/json' \
  -d @examples/generated/11_vote_blockY.json

# 非法输入（均应 422）
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/votes \
  -H 'Content-Type: application/json' -d @examples/generated/20_bad_signature.json
curl -s -o /dev/null -w '%{http_code}\n' -X POST localhost:8000/votes \
  -H 'Content-Type: application/json' -d @examples/generated/21_cross_chain_replay.json
```

示例文件说明：`10+11` 与 `12+13`（逆序）归并为同一 evidence_id；
`14` 是相同投票重复（非双签）；`20/21/22` 分别是坏签名/跨链重放/篡改。

---

## 6. 关键设计取舍

- **链身份进签名体**：跨链混用不需要任何状态判断，密码学直接拒绝，无法被配置绕过。
- **证据内容寻址 + 组唯一约束**：既保证逆序同 ID，又保证一个违规组永远只有一个可罚案件。
- **咨询锁而非"先查后插"**：并发双签在数据库层串行归并，唯一约束是最后防线，不靠应用层侥幸。
- **证据/处罚分事务**：把"崩溃窗口"显式建模为证据状态，启动恢复是普通业务路径，无特殊恢复代码。
- **处罚复制冻结基数**：即使快照行未来被误改，处罚行中的 `frozen_power` 仍是历史事实；
  同时快照写入被设计成不可变，双重保证。
