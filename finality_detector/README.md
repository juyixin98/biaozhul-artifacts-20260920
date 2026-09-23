# Finality Divergence Detector（终局性分歧检测器）

纯后端服务：Python + FastAPI + SQLite。实现一个简化的加权投票终局性检测器，
对「同一位置出现两个相互冲突的已终局值」进行检测、报警并冻结推进。

## 安全规则（全部真实执行，整数比较，无浮点）

1. **验证者集合按 epoch 固定**：`POST /epochs` 创建后不可更改（重复创建返回 409）。
2. **终局判据**：某 value 的签名权重 `power` 满足 `3 * power > 2 * total_weight`
   （严格大于 2/3，纯整数比较）才终局。恰好 2/3 **不**终局。
3. **真实签名**：每票都是 Ed25519 签名，签在带域分隔的规范化消息
   `fdd/vote/v1 | chain_id | epoch | height | value`（长度前缀编码）上。
   签名把 epoch 绑进消息，**跨 epoch 重放必然验签失败**。
4. **重复投票只计一次**：同一验证者对同一 value 重复签名是幂等的。
5. **双投（equivocation）**：同一验证者在同一 (epoch, height) 签出**不同** value，
   两条签名都作为冲突证据持久化，该验证者的**全部权重**在本 epoch 被排除
   （对任何 value 都不计票）。排除不跨 epoch。
6. **epoch 之间不混算**：每个 epoch 独立计票、独立终局。
7. **终局不可覆盖**：某 (epoch, height) 一旦终局，检查点永不重算、永不被新消息覆盖。
8. **终局冲突 → 报警 + 冻结**：若出现另一份针对**不同** value 且权重同样
   超过 2/3 的有效证书（`POST /certificates`），服务记录 `finality_conflict`
   报警（含全部签名证据），冻结该 epoch：拒绝后续投票（409），并禁止创建
   后续 epoch（409 `predecessor_frozen`）。已终局的值保持不变。
9. **持久化与重建**：投票、证据、检查点、报警全部写入 SQLite（WAL，事务提交）。
   重启后从原始投票重算每个 epoch 的结论（`GET /rebuild`），结论与重启前一致。

## 目录结构

```
finality_detector/
├── app/
│   ├── config.py      # 环境变量配置（FDD_DB_PATH / FDD_CHAIN_ID / FDD_RESET_ON_START）
│   ├── crypto.py      # Ed25519 签名/验签、规范化消息
│   ├── db.py          # SQLite 存储层（事务、schema）
│   ├── service.py     # 计票、终局判定、双投排除、冲突报警/冻结、重建
│   └── main.py        # FastAPI 路由
├── examples/
│   ├── gen_keys.py        # 生成演示验证者密钥对
│   ├── sign_vote.py       # 用私钥签一张票
│   ├── demo.py            # 全流程演示（仅标准库，对运行中的服务发真实请求）
│   └── sample_inputs.json # 带真实签名的示例请求体
├── tests/             # pytest 套件（20 个测试）
├── requirements.txt   # 顶层依赖（精确版本）
└── requirements.lock  # 全量锁定（含传递依赖）
```

## 本地启动

```bash
cd finality_detector
python3 -m venv ../.venv
../.venv/bin/pip install -r requirements.lock   # 或 requirements.txt

# 启动服务（默认库文件 finality_detector/data/finality.db）
../.venv/bin/uvicorn app.main:app --port 8000

# 可选环境变量：
#   FDD_DB_PATH=/tmp/fdd.db        数据库路径
#   FDD_CHAIN_ID=my-chain          链标识（参与签名域）
#   FDD_RESET_ON_START=true        启动时清空数据库
```

## 验收命令

```bash
# 1. 自动化测试（少一票 / 恰好2/3 / 零权重 / 跨epoch / 双投 / 冲突冻结 / 重启重建）
cd finality_detector && ../../.venv/bin/python -m pytest -v   # 或 ../.venv/bin/python -m pytest -v

# 2. 端到端演示（先启动服务，再运行）
FDD_DB_PATH=/tmp/fdd-demo.db FDD_RESET_ON_START=true ../.venv/bin/uvicorn app.main:app --port 8000 &
python3 examples/demo.py http://127.0.0.1:8000

# 3. 重启持久化验证：重启服务后
curl -s http://127.0.0.1:8000/rebuild    # 结论与重启前一致
```

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET  | `/health` | 健康检查 + 启动时重建报告 |
| POST | `/epochs` | 创建 epoch（验证者集合随即冻结） |
| GET  | `/epochs/{id}` | epoch 状态：权重、计票、排除、终局值、未决报警 |
| POST | `/votes` | 提交一张签名投票 |
| POST | `/certificates` | 提交外部法定人数证书，检测终局冲突 |
| GET  | `/epochs/{id}/evidence` | 双投证据（两条签名原文） |
| GET  | `/alarms` | 报警列表（可按 `?epoch_id=` 过滤） |
| GET  | `/rebuild` | 从持久化投票重算全部结论 |

错误统一返回 `{"error": <code>, "message": ..., "details": ...}`，主要错误码：
`bad_signature`(400)、`not_validator`(403)、`unknown_epoch`(404)、
`epoch_exists`/`epoch_frozen`/`predecessor_frozen`(409)、
`height_mismatch`/`invalid_public_key`/`negative_weight`(422)。

## 使用示例输入

`examples/sample_inputs.json` 内含一组真实 Ed25519 密钥与签名（chain_id 为
`demo-chain`），依次 POST 即可复现：终局 → 双投排除 → 冲突报警冻结。
也可以自己生成：

```bash
python3 examples/gen_keys.py 4 --weights 40,30,20,10   # 生成验证者与私钥
python3 examples/sign_vote.py 0 100 block-A validator-1 <私钥base64>
```

## 测试覆盖

- `test_quorum.py`：少一票不终局、恰好 2/3 不终局（严格大于）、零权重不计票、
  全零权重永不终局、重复票只计一次
- `test_equivocation.py`：双投存证 + 权重排除、排除持续生效、排除不跨 epoch
- `test_epochs.py`：跨 epoch 签名重放被拒、计票独立、集合冻结、未知 epoch/
  验证者、高度不符、坏签名
- `test_finality_conflict.py`：冲突证书 → 报警 + 冻结 + 终局值不变、冻结后
  拒票拒推进、不足法定人数的证书不报警、一致证书通过、坏签名证书被拒
- `test_persistence.py`：同一数据库文件「重启」后状态、证据、报警逐字节一致，
  冻结仍然生效
