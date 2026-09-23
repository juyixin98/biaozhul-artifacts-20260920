# 终局性分歧检测器（Finality Divergence Detector）

纯后端服务：按 epoch 固定验证者集合的简化加权投票终局检测。合法签名投票权**严格超过**总权重 2/3 才终局；检测到终局分歧（两个块各自获得 >2/3 投票）时告警并冻结推进，已固化的检查点永不回滚。

技术栈：Python 3.12 · FastAPI · SQLite · Ed25519（`cryptography` 库，真实签名验证）。

## 协议规则

1. **验证者集合按 epoch 固定**：`POST /epochs` 一次性登记 `(id, weight, public_key)`，之后不可变更；不同 epoch 的投票绝不混算（签名消息内绑定 epoch，跨 epoch 重放验签必失败）。
2. **投票**：`{epoch, validator_id, block_hash, signature}`。签名消息为域分离的规范编码：
   `lp("p007-finality-vote-v1") || lp(epoch_be) || lp(block_hash_utf8)`，其中 `lp(x)` 为 8 字节大端长度前缀 + 内容。Ed25519 真实验签，失败即拒收。
3. **重复投票**：同一验证者对同一块的重复票幂等拒绝，只计一次。
4. **双投（equivocation）**：同一验证者对不同块投票。两条票都**落库留证**；发现时其权重从**有效计票**中追溯移除（明确排除规则）。**表观计票**保留全部投票，用于观察冲突。
5. **终局判定**：纯整数比较 `3 * votes_weight > 2 * total_weight`（无浮点）。零权重验证者计入总权重、其票不计权；总权重为 0 的 epoch 永不能终局。检查点在有效计票**首次越线**时固化，之后任何消息都不能覆盖或回滚。
6. **终局冲突**：已终局后，另一个块在表观计票中同样越过 2/3 —— 第二个 2/3 利益集合只能由双投产生，属安全故障。服务记录完整证据（两个块、各自投票 id、双投者列表）、将 epoch 置为 `frozen_conflict` 并**冻结**：后续投票一律拒绝（HTTP 409），检查点保持原样。
7. **持久化与重建**：投票 append-only 落 SQLite；checkpoint/conflict 为派生态的持久化记录。每次启动对所有 epoch 重放全部投票并与库中记录**严格核对**，不一致则拒绝启动（fail-fast）。因此重启必然重建同一结论。

## 目录结构

```
app/
  crypto.py    # Ed25519 签名/验签、域分离消息编码
  finality.py  # 严格超多数的纯整数判定
  schemas.py   # Pydantic 请求/响应模型
  storage.py   # SQLite schema 与读写（WAL、外键、立即事务）
  service.py   # 投票处理、双投检测、重放、冲突冻结、启动核对
  main.py      # FastAPI 路由
tests/         # pytest：阈值边界 / 零权重 / 跨 epoch / 双投 / 冲突 / 持久化 / 密码学
examples/      # 带真实签名的示例输入（scripts/gen_examples.py 确定性生成）
scripts/
  gen_examples.py  # 重新生成示例
  demo.sh          # 端到端验收演示（含重启重建验证）
```

## 本地启动

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt   # 锁定依赖
.venv/bin/uvicorn app.main:app --port 8000
```

数据库路径由环境变量 `FINALITY_DB` 指定（默认 `./finality.db`）。交互式 API 文档：`http://127.0.0.1:8000/docs`。

## 验收命令

```bash
.venv/bin/python -m pytest -q     # 21 个自动化测试
bash scripts/demo.sh              # 端到端演示：正常终局 / 冲突冻结 / 零权重 / 重启重建
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查；返回启动时核对通过的 epoch 列表 |
| POST | `/epochs` | 登记 epoch 与固定验证者集合（重复登记 409） |
| GET | `/epochs` | epoch 摘要列表 |
| GET | `/epochs/{epoch}/status` | 完整状态：有效/表观计票、双投、检查点、冲突证据 |
| POST | `/votes` | 提交签名投票；拒绝时返回 `{accepted:false, reason}` |
| GET | `/checkpoints` | 全部终局检查点 |
| GET | `/epochs/{epoch}/evidence` | 双投与冲突的可独立验证证据（含原始签名票） |

### 示例（curl）

```bash
# 登记 epoch（examples/epoch1.json：alice/bob/carol 各权重 1）
curl -X POST localhost:8000/epochs -H 'Content-Type: application/json' -d @examples/epoch1.json

# 逐张提交投票（examples/votes_epoch1.json 内含真实签名）
curl -X POST localhost:8000/votes -H 'Content-Type: application/json' \
  -d "$(python3 -c "import json;print(json.dumps(json.load(open('examples/votes_epoch1.json'))[0]))")"

# 查看状态与证据
curl localhost:8000/epochs/1/status
curl localhost:8000/epochs/2/evidence   # 冲突场景的双投证据
```

## 测试覆盖（对应需求）

- **少一票**：权重 3/3/3（总 9），6 票不终局，第 7 票终局（`test_one_vote_short`）。
- **恰好 2/3**：3×权重 1，2 票（=2/3）不终局，3 票才终局；权重 10/10/10/1 时 20 票不够、21 票终局（`test_exactly_two_thirds`、`test_weighted_threshold_boundary`）。
- **零权重**：零权重票合法接收但不计权、无法助推终局；全零权重 epoch 永不终局（`test_zero_weight.py`）。
- **跨 epoch**：投票不混算；签名绑定 epoch，跨 epoch 重放验签失败（`test_epochs.py`）。
- **重复/双投**：重复票只计一次；双投留证并排除权重（`test_equivocation.py`）。
- **终局冲突**：第二 2/3 联盟触发告警+冻结，检查点不被覆盖，冻结后投票被拒（`test_conflict.py`）。
- **持久化**：重启重放重建同一结论（含冻结态）；篡改 checkpoint 启动即失败（`test_persistence.py`）。
- **密码学**：真实 Ed25519 往返、错块/错 epoch/错钥/畸形输入全部验签失败（`test_crypto.py`）。
