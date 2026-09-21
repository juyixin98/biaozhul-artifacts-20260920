# TargetCraft — 本地广告投放决策后端

Go + Gin + GORM + MySQL 实现的广告投放决策服务：活动/创意管理、定向规则匹配、
预算与频次的原子预占、展示确认结算、到期自动释放与崩溃恢复。不接真实广告渠道。

## 快速开始（Docker）

```bash
docker compose up --build
```

服务监听 `:8080`，启动时自动执行迁移并写入样例数据（活动 ID=1，创意 ID=1/2）。
一键演示完整流程：

```bash
./scripts/demo.sh
```

本地开发（需要可访问的 MySQL 8）：

```bash
export DATABASE_DSN='root:root@tcp(127.0.0.1:3306)/targetcraft?charset=utf8mb4&parseTime=true&loc=UTC'
export SEED_SAMPLE_DATA=true
go run ./cmd/server
```

## 核心语义

- **活动（Campaign）**：投放起止时间、整数分总预算、每日上限（`daily_cap`，0 表示不限）、
  定向规则（地区 / 设备 / UTC 时段，空数组表示不限）。
- **创意（Creative）**：只能投放 `active` 状态且属于该活动的创意；不指定 `creative_id`
  时自动选择该活动第一个可用创意。拒绝时返回具体原因
  （`campaign_not_active`、`region_not_targeted`、`user_hourly_frequency_exceeded` 等）。
- **决策请求**：`request_id`（幂等键）、`user_id`、`occurred_at`、`cost`（整数分）、
  `region`、`device`。预算按 **UTC 自然日** 统计，频次按 **UTC 自然小时** 统计：
  每用户每小时 ≤ 3 次，每活动每天 ≤ 20 次。
- **原子预占**：决策通过时在单个事务内用条件 UPDATE（`reserved + spent + cost <= cap`、
  `count < max`）原子占用总预算、日预算、两类频次计数，并发下不会超限。
- **幂等**：相同 `request_id` + 相同内容重放返回原结果（含拒绝结果）；
  相同 ID 不同内容返回 **409 Conflict**。
- **确认 / 释放**：确认把预占变为实际消耗（reserved → spent）；5 分钟未确认由
  清扫器释放并全额退回预算与频次。状态迁移是条件 UPDATE，确认与到期竞争只有一个成功；
  重复确认幂等返回，过期确认返回 **410 Gone**。
- **恢复**：所有状态在 MySQL 中，进程重启后启动清扫即可回收遗留预占，
  并清理崩溃残留的 `evaluating` 记录，不依赖内存定时器。
- **暂停**：活动暂停立即拒绝新决策，但已成功的预占在有效期内仍可确认。

## API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/campaigns` | 创建活动（含定向规则） |
| GET  | `/api/campaigns/:id` | 活动详情 + 当日预算 |
| POST | `/api/campaigns/:id/pause` `/resume` | 暂停 / 恢复 |
| POST | `/api/campaigns/:id/creatives` | 创建创意 |
| POST | `/api/creatives/:id/pause` `/resume` | 创意上下线 |
| POST | `/api/decisions` | 决策请求（幂等） |
| GET  | `/api/decisions?campaign_id=&user_id=&status=` | 决策记录查询 |
| GET  | `/api/decisions/:request_id` | 单条决策 |
| POST | `/api/decisions/:request_id/confirm` | 展示确认（结算） |
| GET  | `/api/settlements?campaign_id=&day=` | 结算记录（已确认） |

决策请求示例：

```json
{
  "request_id": "req-001",
  "user_id": "user-42",
  "campaign_id": 1,
  "creative_id": 0,
  "region": "CN",
  "device": "ios",
  "occurred_at": "2026-09-20T15:04:05Z",
  "cost": 150
}
```

## 配置（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `PORT` | `8080` | HTTP 端口 |
| `DATABASE_DSN` | 见 `internal/config` | MySQL DSN |
| `RESERVATION_TTL` | `5m` | 预占有效期 |
| `SWEEP_INTERVAL` | `1s` | 到期清扫周期 |
| `RUN_MIGRATIONS` | `true` | 启动时自动迁移 |
| `SEED_SAMPLE_DATA` | `false` | 写入样例数据 |

## 测试

```bash
# 默认连接 127.0.0.1:3306（root 无密码），可用环境变量覆盖：
export TARGETCRAFT_TEST_DSN='root:root@tcp(127.0.0.1:3306)/?charset=utf8mb4&parseTime=true&loc=UTC'
go test ./... -count=1
```

覆盖重点：并发预算不超限、用户小时/活动日频次边界、日预算上限、幂等重放与 409 冲突、
重复确认不双扣、确认与到期竞争只成功一方、过期确认拒绝、跨日预占按决策日结算、
进程重启后恢复未处理预占、活动暂停语义、定向规则各拒绝原因。
