# TargetCraft

本地广告投放决策后端：Go + Gin + GORM + MySQL。不接真实广告渠道，不做素材处理与拍卖，只负责**决策、预算/频控的原子预占、展示确认结算与到期释放**。

## 快速开始（Docker）

```bash
# 宿主机 3306/8080 常被占用，默认映射到 28080(api)/33306(mysql)，可用环境变量覆盖
sudo env API_HOST_PORT=28080 MYSQL_HOST_PORT=33306 docker compose up -d --build

curl localhost:28080/healthz        # {"status":"ok"}
BASE=http://localhost:28080 bash scripts/sample.sh   # 端到端样例
```

启动时自动执行迁移（`internal/db.Migrate`），并先做一次到期清扫完成**重启恢复**，之后每 `SWEEP_INTERVAL`（默认 10s）增量清扫。

## 本地开发与测试

```bash
scripts/test-mysql.sh up   # 启动一次性测试库 root/rootpass @ 127.0.0.1:13306
TEST_MYSQL_DSN='root:rootpass@tcp(127.0.0.1:13306)/?charset=utf8mb4&parseTime=true&loc=UTC' \
  go test ./... -race -count=1
```

测试重点覆盖：并发预算不超支、每用户每小时 3 次 / 每活动每天 20 次频控边界、
幂等重放与同 ID 冲突、重复确认不双扣、到期释放与过期确认 410、跨日预占、
确认与到期竞争只成一个、重启恢复、暂停活动阻止新决策。

## API 一览（前缀 `/api/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/campaigns` | 创建活动（起止时间、整数分总预算/每日上限、地区/设备/时段定向） |
| GET | `/campaigns/:id` | 查询活动（含累计预占/消耗） |
| POST | `/campaigns/:id/status` | `{"status":"active"\|"paused"}` 暂停/恢复 |
| POST | `/creatives` | 创建创意（`campaign_id`、`name`） |
| POST | `/decisions` | 广告决策（幂等） |
| POST | `/decisions/:request_id/confirm` | 展示确认（幂等） |
| GET | `/decisions/:request_id` | 单条决策记录 |
| GET | `/decisions?campaign_id=&user_id=&status=&day=&page=&page_size=` | 决策记录查询 |
| GET | `/settlements?campaign_id=&day=&type=` | 结算记录查询（`type=confirm\|release`） |

### 决策请求

```json
{
  "request_id": "唯一请求ID（幂等键）",
  "creative_id": 1,
  "user_id": "user-1",
  "region": "CN",
  "device": "ios",
  "occurred_at": "2026-09-20T12:00:00Z",
  "cost_cents": 150
}
```

- 通过：`200 {"accepted":true,"status":"reserved","expires_at":...}`，同一事务内原子预占预算与频次。
- 业务拒绝：**HTTP 200** `{"accepted":false,"reject_code":"REGION_NOT_MATCHED","reject_reason":"..."}`，
  拒绝同样落库、可幂等重放。拒绝码：`CREATIVE_NOT_FOUND` / `CAMPAIGN_NOT_FOUND` /
  `CREATIVE_NOT_ACTIVE` / `CAMPAIGN_NOT_ACTIVE` / `CAMPAIGN_NOT_STARTED` / `CAMPAIGN_ENDED` /
  `REGION_NOT_MATCHED` / `DEVICE_NOT_MATCHED` / `TIME_WINDOW_NOT_MATCHED` /
  `TOTAL_BUDGET_EXHAUSTED` / `DAILY_CAP_EXCEEDED` / `CAMPAIGN_DAILY_CAP_REACHED` / `USER_HOURLY_CAP_REACHED`。
- 相同 `request_id` 重复提交（内容一致）→ 返回首次响应原文；内容不一致 → **409 `REQUEST_ID_CONFLICT`**。

### 展示确认

- 预占后 5 分钟（`RESERVATION_TTL`）内确认：`reserved → confirmed`，预占转实际消耗，写 `confirm` 结算记录。
- 重复确认返回首次确认结果，不双扣。
- 超时未确认由清扫器（或迟到的确认自身）释放：计数器回退、写 `release` 结算记录；过期确认返回 **410**。
- 确认与到期清扫都用 `UPDATE ... WHERE status='reserved'` 条件更新，以影响行数定归属——**只会有一个成功**。

## 关键设计（改动必须保持）

1. **金额一律整数分，时间一律 UTC**（`datetime(3)` 毫秒精度，写入前 `Truncate(time.Millisecond)`）。
2. **预算按 UTC 日**（`budget_days`，以 `occurred_at` 所在 UTC 日计）、**用户频次按 UTC 自然小时**
   （`user_hours`，跨活动每用户每小时 3 次；每活动每天 20 次展示）。
3. 决策通过在**单事务**内按固定锁序 `campaign → creative → budget_day → user_hour` 加
   `FOR UPDATE` 行锁后判定并增量；计数器行先 `ON DUPLICATE KEY UPDATE` 再锁。并发不超支、不超频。
4. 预占状态机 `reserved → confirmed/released`，条件更新 + RowsAffected 保证竞争只成一个。
5. **恢复只依赖数据库**：`SweepExpired` 查 `status='reserved' AND expires_at<=now`，启动先扫一次，
   ticker 仅加速；进程内存不持有权威状态。
6. 暂停活动立即阻止新决策；已成功预占在有效期内仍可确认（确认不检查活动状态）。
7. 跨日预占：预算/结算始终记在 `occurred_at` 所在 UTC 日，确认跨午夜不错账。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `HTTP_ADDR` | `:8080` | 监听地址 |
| `MYSQL_DSN` | `root:rootpass@tcp(127.0.0.1:33306)/targetcraft?...` | MySQL DSN |
| `SWEEP_INTERVAL` | `10s` | 到期清扫间隔 |
| `RESERVATION_TTL` | `5m` | 预占有效期 |
| `TEST_MYSQL_DSN` | — | 集成测试库（不设则跳过测试） |
