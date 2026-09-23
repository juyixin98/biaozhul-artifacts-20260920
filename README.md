# 传感器健康判定后端 (Sensor Health Determination)

纯后端服务：接收**合成传感器消息**，基于滑动窗口判定三类健康问题，告警进入与
恢复使用**不同阈值（迟滞）**，全部状态落 SQLite、**跨重启保持**。Go + net/http
+ SQLite（`modernc.org/sqlite`，纯 Go，无 cgo 依赖）。

## 三类检测规则

| 规则 | 进入阈值 | 恢复阈值 | 判定依据 |
|---|---|---|---|
| `stale` 停更 | 超过 `stale_enter_sec` 未收到**任何**消息（数据或心跳） | 收到新消息即恢复 | **服务接收时间** `recv_at`，与设备采样时间严格区分；心跳算存活 |
| `fixed_value` 固定值 | 连续 `fixed_window_count` 个数据样本值相等（容差 `fixed_tolerance`） | 值发生真实变化即恢复 | 数据消息的值序列；心跳不参与 |
| `sequence_gap` 序号缺口 | 前向序号跳跃，`newSeq-lastSeq>1`，记录缺失区间 | 连续 `gap_recover_gapless` 个无缺口样本 | 每个时钟纪元内的单调序号 |

关键设计：

- **不能把静止设备一律判坏**：固定值阈值**按设备类型配置**。门磁、开关等二值
  设备配置 `fixed_window_count: 0` 即彻底关闭固定值检测，长时间为 0/1 完全正常；
  温度类连续量才配置窗口。内置种子类型：`temperature` / `door` / `switch`。
- **设备采样时间 vs 服务接收时间 vs 心跳**：消息同时携带设备 `sample_time`
  （RFC3339），服务端在接收时刻另盖 `recv_at`；心跳是无值的纯存活消息
  （`is_heartbeat: true`）。停更只看接收时间与心跳，固定值/缺口只看数据消息。
- **时钟回跳开启新序列**：`sample_time` 向后跳超过 `clock_skew_tol_ms` 视为
  设备重启/对时，`epoch + 1`、序号从头开始**不算缺口**；回跳容差内的小抖动
  （如 300ms）被当作抖动吸收，不开新纪元。
- **批量补发 vs 时钟回跳的协议区分**：二者在线上都表现为"旧时间戳+小序号"，
  无法靠报文形态区分。摄取请求显式标注 `"mode": "backfill"`：补发批只入库、
  标记 `backfill:true`、**不移动序号高水位线、不触发任何规则**（但仍算存活）；
  默认 `"live"` 流中出现旧时间戳+序号重启才判为时钟回跳。
- **告警返回触发样本区间与配置版本**：每个事件记录
  `start_seq/end_seq`、`start_sample/end_sample`（触发样本区间）、缺口事件额外
  记录 `gap_start/gap_end`（缺失序号范围），并带 `config_version`（进入时版本）
  与 `recovery_config_version`（恢复时版本）。
- **密码操作真实执行**：摄取口用 **HMAC-SHA256**（密钥对 `timestamp\nbody`
  签名，常量时间比较，5 分钟重放窗口）；告警 webhook 同样真实 HMAC 签名，
  `sensorctl recv` 会真实验签。密钥由 `crypto/rand` 生成。

## 目录结构

```
cmd/server            HTTP 服务入口（虚拟/真实时钟、后台停更扫描、优雅关停）
cmd/sensorctl         CLI：签名、发送、webhook 接收验签、生成随机密钥
internal/model        领域模型与配置校验
internal/clock        可注入时钟（Real / Virtual）
internal/cryptox      HMAC-SHA256 签名/验签/随机密钥
internal/store        SQLite：建表、种子配置、全部读写（事务）
internal/engine       三类窗口检测、迟滞、时钟纪元、补发、持久化/恢复
internal/webhook      告警投递（真实签名、重试、投递记录落库）
internal/httpapi      JSON HTTP API（摄取鉴权、查询、管理）
examples/             示例输入
scripts/acceptance.sh 一键端到端验收（33 项检查）
```

## 本地启动

需要 Go 1.22+（无需 C 编译器，SQLite 驱动为纯 Go）。

```bash
# 1. 下载锁定依赖
go mod download

# 2. 构建
go build ./...

# 3. 准备密钥（或让服务在未设置时生成一次性密钥并打印到日志）
export SENSOR_INGEST_SECRET=$(go run ./cmd/sensorctl gen-secret)
export SENSOR_ADMIN_TOKEN=$(go run ./cmd/sensorctl gen-secret)

# 4. 启动（默认虚拟时钟 :8080，数据库 ./sensorhealth.db）
go run ./cmd/server \
  --addr :8080 --db sensorhealth.db --clock virtual \
  --ingest-secret "$SENSOR_INGEST_SECRET" --admin-token "$SENSOR_ADMIN_TOKEN"
```

真实时钟模式（生产）用 `--clock real`；虚拟时钟模式可用管理接口快进时间，
便于确定性地演示停更检测。

可选 webhook（告警进入/恢复都会签名 POST 过去）：

```bash
# 终端 A：起一个会真实验签的接收端
go run ./cmd/sensorctl recv --addr :9090 --secret "$SENSOR_WEBHOOK_SECRET"

# 终端 B：启动服务并配置 webhook
go run ./cmd/server --clock real \
  --ingest-secret "$SENSOR_INGEST_SECRET" --admin-token "$SENSOR_ADMIN_TOKEN" \
  --webhook-url http://127.0.0.1:9090/ --webhook-secret "$SENSOR_WEBHOOK_SECRET"
```

## 验收命令

```bash
# 全部自动化单元/集成测试（含重启持久化、抖动、补发、HMAC 验签等）
go test ./... -count=1 -v

# 一键端到端验收（真实构建、真实 HTTP、真实 SQLite、真实 webhook 验签，33 项）
./scripts/acceptance.sh
```

## 手动试一把

```bash
# 用 CLI 对示例报文做真实 HMAC 签名并发送（虚拟时钟下用服务端时钟签名）
NOW=$(curl -s -H "Authorization: Bearer $SENSOR_ADMIN_TOKEN" \
  http://127.0.0.1:8080/admin/clock | jq -r .now)
go run ./cmd/sensorctl post --url http://127.0.0.1:8080 \
  --secret "$SENSOR_INGEST_SECRET" --file examples/normal.json --timestamp "$NOW"

# 查询设备健康
curl -s http://127.0.0.1:8080/v1/devices | jq .

# 快进 35 秒触发 temperature 停更（其默认进入阈值 30s），随后自动扫描告警
curl -s -X POST -H "Authorization: Bearer $SENSOR_ADMIN_TOKEN" \
  -H 'Content-Type: application/json' -d '{"advance_ms":35000}' \
  http://127.0.0.1:8080/admin/clock/advance | jq .

# 发一个心跳恢复停更
go run ./cmd/sensorctl post --url http://127.0.0.1:8080 \
  --secret "$SENSOR_INGEST_SECRET" --file examples/heartbeat.json --timestamp "$NOW"

# 查事件（触发样本区间、缺失序号、配置版本）
curl -s "http://127.0.0.1:8080/v1/events?device_id=temp-room-a" | jq .
```

## HTTP 接口

### `POST /v1/ingest`（HMAC 鉴权）

请求头：

- `X-Timestamp: <RFC3339>` — 必须在服务端 ±5 分钟内（防重放）
- `X-Signature: sha256=<hex>` — `hex(HMAC_SHA256(secret, timestamp_unix + "\n" + raw_body))`

请求体：

```json
{
  "mode": "live",                 // "live"（默认）或 "backfill"（有序历史补发）
  "messages": [
    {
      "device_id": "temp-room-a",
      "type": "temperature",
      "seq": 12,                  // 数据消息可选；心跳无序号
      "sample_time": "2026-09-23T10:00:00Z",  // 设备采样时间
      "value": 21.5               // 数据消息必填；心跳省略
    },
    {
      "device_id": "temp-room-a",
      "type": "temperature",
      "sample_time": "2026-09-23T10:00:05Z",
      "is_heartbeat": true        // 纯存活心跳，无 value
    }
  ]
}
```

响应逐条给出 `accepted/duplicate/backfill/new_epoch` 等结果。

### 查询接口（只读，无鉴权）

- `GET /v1/devices` — 全部设备健康
- `GET /v1/devices/{id}` — 单设备健康（三规则当前状态、版本、时钟纪元）
- `GET /v1/devices/{id}/messages?limit=N` — 已入库原始消息（含 `recv_at`）
- `GET /v1/events?device_id=&rule=&open=true|false&limit=` — 告警事件
- `GET /v1/config/device-types` — 设备类型配置与全局版本
- `GET /healthz`

### 管理接口（Bearer Token）

- `PUT /admin/config/device-types/{type}` — 创建/更新类型阈值，全局配置版本号 +1
- `GET  /admin/clock` — 当前时钟与模式
- `POST /admin/clock/advance` — 仅虚拟时钟：`{"advance_ms":35000}` 快进并立即
  执行一次停更扫描（`"sweep":false` 可只推进不扫描）

配置体示例见 `examples/device_type_temperature.json`。校验规则：
`0 < stale_recover_sec < stale_enter_sec`（强制迟滞）、`fixed_window_count >= 0`
（0 表示对该类型关闭固定值检测）。

## 数据如何持久化

- `device_types` / `meta(global_version)`：配置与单调版本号
- `devices`：每设备投影（纪元、序号高水位线、最近采样/心跳/接收时间）
- `messages`：全部原始消息（含服务端 `recv_at`）
- `device_states`：引擎每设备每规则状态（JSON，每次变更随事务写回）
- `events`：告警生命周期（进入一行，恢复时关闭同一行；区间、缺失范围、双版本）
- `webhook_deliveries`：每次投递尝试（含失败原因），投递失败不影响事件落库

重启时从 SQLite 重建内存状态，`go test` 中的 `TestStateSurvivesRestart` 与
验收脚本的 restart 段会真实杀进程、重开同一库文件验证。

## 示例输入

- `examples/normal.json` — 正常数据（温度类 + 静止门磁）
- `examples/clock_rollback.json` — 设备时钟回跳、序号重启（触发新纪元）
- `examples/backfill.json` — `"mode":"backfill"` 有序历史补发
- `examples/heartbeat.json` — 纯心跳
- `examples/device_type_temperature.json` — 类型阈值配置
