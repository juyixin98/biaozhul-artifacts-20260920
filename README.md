# 渐进发布判定器（Progressive Rollout Arbiter）

纯后端的本地渐进发布决策系统：按 **5% → 20% → 50% → 100%** 四个阶段放量，
每个阶段必须同时满足**最小样本量**与**完整观察窗口**，且错误率 / P95 延迟
指标达标，才允许推进。指标来自独立的**桩服务（stub）**，缺指标判 **unknown（未知）**
而不是健康；阈值版本在发布开始时**冻结**；所有命令带**预期代次（generation）**做
乐观并发控制，并用**幂等键 + SHA-256 请求指纹**保证重试不重复执行。

技术栈：Go 1.22 · Chi · PostgreSQL（pgx/v5）。所有计算、协议往返与哈希均真实执行。

## 目录结构

```
cmd/server/         rollout-api HTTP 服务（:18082）
cmd/metrics-stub/   受控指标桩服务（:18081）
internal/engine/    纯判定逻辑（无 I/O，单元测试覆盖）
internal/stub/      桩服务：5 类受控场景 + 迟到/订正数据
internal/metrics/   桩服务 HTTP 客户端（204/错误 → 指标缺失）
internal/store/     PostgreSQL 持久层（事务 + 行锁 + 幂等表）
internal/api/       Chi 路由：判定、命令幂等、代次校验、证据落库
migrations/         SQL schema
examples/           示例输入 JSON
scripts/            建库、本地启动、验收演示脚本
```

## 判定规则

判定在每阶段区间 `[stage_started_at, stage_started_at+window)` 上进行，顺序如下：

1. **无任何指标 / 拉取失败** → `unknown`（`metrics_missing`）——绝不视为健康；
2. **已闭合时间桶未全部到齐**（部分覆盖）→ `unknown`（`metrics_partial`）；
3. 观察窗口时间未走完 → `hold`（`observation_window_incomplete`）；
4. 样本数 < `min_samples` → `hold`（`insufficient_samples`）；
5. 聚合错误率 > 阈值 或 加权 P95 > 阈值 → `violation`（持续退化，阻断推进）；
6. 全部满足 → `promote`。区间内**短暂尖峰**若未把整窗聚合值推过阈值，不阻断。

错误率 = Σerrors/Σsamples；P95 为各桶 P95 按样本量加权。每个判定都把完整的
**时间桶区间证据**（含每桶 samples/errors/p95/ingested_at、覆盖率、窗口边界、
冻结阈值版本号）存入 `decisions` 表并随接口返回。

## HTTP 协议

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/rollouts` | 创建发布；同时冻结一份阈值版本 |
| GET | `/rollouts` / `/rollouts/{id}` | 列表 / 详情（含当前阶段权重、代次） |
| POST | `/rollouts/{id}/evaluate` | 拉取桩服务指标做一次只读判定并落证据 |
| POST | `/rollouts/{id}/commands` | `promote` / `pause` / `rollback` 命令 |
| GET | `/rollouts/{id}/decisions` | 判定证据历史（倒序） |

命令体：

```json
{ "type": "promote", "idempotency_key": "唯一键", "expected_generation": 0 }
```

- 代次不匹配 → `409 rejected_conflict`，不执行；
- 状态不允许或推进前判定不通过 → `409 rejected_state`，判定证据随响应返回；
- 同一 `idempotency_key` 重试 → 返回首次结果（响应头 `Idempotent-Replay: true`），
  代次不重复递增；
- 同键但请求体不同（SHA-256 指纹不一致）→ `409` 拒绝。

桩服务：`POST /scenario` 设置场景，`GET /metrics?service=&from=&to=` 取时间桶，
`GET /scenarios` 列出全部场景。

## 本地启动（Ubuntu + PostgreSQL 16）

```bash
# 1. 建库建角色 + 应用迁移（需要 sudo 切到 postgres 系统账号）
./scripts/setup_db.sh

# 2. 构建并后台启动两个服务（18082 / 18081）
./scripts/run_local.sh
```

环境变量（均有默认值）：`DATABASE_URL`、`METRICS_URL`、`LISTEN_ADDR`、
`STUB_ADDR`、`MIGRATION_PATH`、`SKIP_MIGRATIONS=1`。

## 验收

一键走查全部受控场景（约 45 秒，含真实等待观察窗口）：

```bash
./scripts/demo.sh
```

覆盖：①健康四阶段推进到 completed；②短暂尖峰不阻断（证据含尖峰桶）；
③持续退化→violation→暂停→回退→重复回退拒绝；④样本不足→hold；
⑤指标中断→unknown 且无指标值；⑥回退后迟到成功（首版 10% 错误触发回退，
订正数据到达后同区间转为健康，两次判定都在证据历史里）；
⑦幂等重试不重复推进、过期代次拒绝、同键不同体拒绝。

自动化测试（端到端测试使用真实 PostgreSQL + httptest 桩服务，默认连
`postgres://rollout:rollout@localhost:5432/rollout_test`，可用
`TEST_DATABASE_URL` 覆盖；库不可达时跳过）：

```bash
go test ./... -v
# 只跑纯逻辑
go test ./internal/engine -v
```

## 受控场景

| 场景 | 指标形态 | 期望判定 |
|---|---|---|
| `healthy` | 0% 错误，p95=120ms，100 请求/桶 | promote |
| `transient_spike` | 第 2 桶 8% 错误 + p95=900ms，随后恢复 | promote（聚合 2%/315ms） |
| `sustained_degradation` | 持续 6% 错误，p95=800ms | violation |
| `insufficient_samples` | 5 请求/桶 | hold |
| `metrics_outage` | 无数据 | unknown（非健康） |
| `late_success` | 首版 10% 错误，`correction_delay_seconds` 后订正为健康 | violation → 迟到 promote |

## 手动调用示例

```bash
curl -s localhost:18081/scenarios | jq .

curl -s -X POST localhost:18081/scenario -H 'Content-Type: application/json' \
  -d '{"service":"checkout","name":"transient_spike","bucket_seconds":1}'

curl -s -X POST localhost:18082/rollouts -H 'Content-Type: application/json' \
  -d @examples/create_rollout.json

# 观察窗口走完后
curl -s -X POST localhost:18082/rollouts/1/evaluate | jq .
curl -s -X POST localhost:18082/rollouts/1/commands \
  -H 'Content-Type: application/json' -d @examples/command_promote.json | jq .
curl -s localhost:18082/rollouts/1/decisions | jq .
```

## 设计要点

- **冻结阈值**：创建发布时向 `threshold_versions` 插入一行并外键关联，判定永远
  使用该版本；事后新增更严格版本不影响在途发布（有测试）。
- **并发与幂等**：命令在单个事务内 `SELECT ... FOR UPDATE` 锁定发布行；幂等表
  对 `(rollout_id, idempotency_key)` 有唯一约束，锁内双重检查，唯一冲突时重放
  对方结果，保证恰好执行一次。
- **未知优先**：指标缺失/部分覆盖的优先级高于窗口与样本判断，且 `error_rate`、
  `p95_latency_ms` 以 SQL `NULL` / JSON `null` 落库，不会出现"无数据=0%=健康"。
- **迟到数据**：桩服务对同一时间桶支持 preliminary/final 两版，final 带到达
  时间；判定方每次实时拉取，回退后仍可对原窗口重新取证，历史判定不可篡改。
