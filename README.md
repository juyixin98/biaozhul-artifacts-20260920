# 传感器健康判定后端（sensorhealth）

纯后端服务：接收合成传感器消息（采样值 / 心跳），用三种窗口规则判定设备健康，
告警进入与恢复采用不同阈值（迟滞），状态持久化到 SQLite，**进程重启后状态保持**。

技术栈：Go 1.22、标准库 `net/http`、SQLite（纯 Go 驱动 `modernc.org/sqlite`，**无需 cgo**）。
所有计算、HMAC-SHA256 密码学校验均真实执行。

---

## 1. 三类时间的区分

每条消息严格区分三种时间，互不混用：

| 字段 | 时钟 | 含义 | 用途 |
|---|---|---|---|
| `sampled_at` | **设备时钟** | 设备实际采样时刻 | frozen（固定值）按设备时间跨度判定 |
| `received_at` | **服务器时钟** | HTTP 服务收到消息的时刻（服务端写入） | stale（停更）判定 |
| `is_heartbeat` | — | 只表示"设备活着"，**不带值、不占序号** | 只为 stale 续命，不参与 frozen/gap |

这样"设备时间乱跳"和"链路延迟/补发"不会互相污染判定。

---

## 2. 三条健康规则

规则阈值**按设备类型（device type）独立配置、带版本号**。静止设备（如门磁）
配置更长/更大的 frozen 阈值，因此**不会因为读数恒定就被一律判坏**。

### 2.1 stale（停更）— 基于服务器接收时间
- 距上次收到**任何消息（采样或心跳）**超过 `stale_enter_timeout` → 告警进入。
- 恢复：恢复连续可达并持续 `stale_recover_timeout`（**短于**进入阈值）后才恢复，
  避免边界抖动反复横跳。心跳可以阻止/恢复停更。

### 2.2 frozen（固定值）— 基于设备采样时间 + 序号
进入需**同时**满足：
- 连续 `frozen_enter_count` 个采样值完全相同，**且**
- 这些采样在**设备采样时间**上跨度 ≥ `frozen_enter_min_duration`，**且**
- 序号持续推进（重复投递同一序号被去重，不会充数）。

恢复：连续 `frozen_recover_count`（**小于**进入数）个不同值才恢复；期间再次
出现冻结值会把恢复计数清零。门磁等类型把进入阈值调到 100 次 / 10 分钟即不告警。

### 2.3 gap（序号缺口）— 基于连续前锋（contiguous frontier）
维护每台设备每个序列（epoch）的：
- `frontier_seq`：从本序列首个序号起**连续无缺口**的最高序号；
- `peak_seq`：已见到的最高序号。开放缺口恰为 `(frontier_seq, peak_seq]`。

- 正向跳变使缺口缺失数 ≥ `missing_enter_count` → 进入，**触发样本区间**为 `[frontier+1, peak]`。
- **批量补发**（乱序到达的历史序号）按序号排序处理，填满连续段就推进 `frontier_seq`，
  缺口缩小到 `< missing_recover_count`（**不同于**进入阈值）即恢复，恢复区间记录补到的序号段。
- 重复序号幂等忽略。

### 2.4 序列分段（epoch）与时钟回跳
以下任一情况开启**新 epoch**，frozen/gap 状态重置、该两类未恢复告警在边界处记为恢复，
告警绝不跨 epoch 串联；stale 因衡量服务器可达性而跨 epoch 保留：
1. **序号大幅回退**（重置到回补窗口之外）；
2. **序号大幅前跳**（超过 `backfill_lookback`，判定为设备重启进入新会话，
   而非成千上万个假缺失）；
3. **设备时钟回跳**：序号仍前进，但 `sampled_at` 相对上一采样后退超过 stale 容差。

小幅采样时间抖动、相同时间戳**不会**开启新 epoch（有测试覆盖）。

---

## 3. 告警返回内容

每个告警（进入/恢复）都带：
- `trigger_range`：触发样本区间 `{epoch, seq_start, seq_end, first_sample_time, last_sample_time}`；
- `recover_range`：恢复区间（恢复后非空）；
- `config_version`：**触发时冻结的规则配置版本**；
- `opened_at` / `recovered_at`、人读 `detail`。

---

## 4. 协议与安全（真实 HMAC-SHA256）

`POST /api/v1/ingest` 的请求体连同 method/path/时间戳用 **HMAC-SHA256** 签名：

```
canonical = METHOD \n PATH \n UNIX_MILLI(timestamp) \n RAW_BODY
X-Signature = hex(HMAC_SHA256(secret, canonical))
```

- 使用 `crypto/hmac.Equal` 恒定时间比较；
- `X-Timestamp` 与服务器时钟偏差超过 5 分钟拒绝（防重放窗口）；
- `X-Nonce` 一次性，缓存于窗口内，重放同一 nonce 返回 `409`；
- 管理端（注册设备、改配置）用独立 Bearer Token。

随附的 `cmd/ingest-client` 对文件字节**真实计算签名并原样发送**，签名与负载不会漂移。

---

## 5. HTTP 接口

| 方法 & 路径 | 鉴权 | 说明 |
|---|---|---|
| `POST /api/v1/ingest` | HMAC 头 | 上报一批采样/心跳 |
| `GET  /api/v1/devices/{id}/health` | 无 | 当前健康快照（含 epoch、配置版本、各规则明细） |
| `GET  /api/v1/alerts?device_id=&status=&kind=` | 无 | 告警历史 |
| `POST /admin/devices` | Bearer | 注册设备（自动播种该类型默认配置） |
| `GET/PUT /admin/device-types/{type}/config` | Bearer | 查看/更新规则配置（更新即版本号 +1，校验迟滞约束） |
| `POST /admin/sweep` | Bearer | 立即跑一次 stale 巡检 |
| `GET  /healthz` | 无 | 存活探针 |

消息体示例（心跳 `kind:"heartbeat"` 且无 seq/value）：

```json
{
  "device_id": "temp-1",
  "messages": [
    {"seq": 1001, "value": "21.5", "sampled_at": "2026-09-23T10:00:00Z"},
    {"kind": "heartbeat", "sampled_at": "2026-09-23T10:00:20Z"}
  ]
}
```

---

## 6. 本地启动

前置：Go ≥ 1.22（无需 C 编译器；SQLite 为纯 Go 实现）。

```bash
# 下载并锁定依赖
go mod tidy

# 直接运行（启动时播种示例类型 temp/contact 与设备 temp-1/contact-1）
make run
# 或：go run ./cmd/sensorhealth -seed
```

可用参数 / 环境变量：`-addr|ADDR`、`-db|DB_PATH`、`-hmac-secret|HMAC_SECRET`、
`-admin-token|ADMIN_TOKEN`、`-sweep-interval|SWEEP_INTERVAL`、`-seed|SEED=1`。

发送一条真实签名消息：

```bash
go run ./cmd/ingest-client -file examples/batch_healthy.json
# 自定义地址/密钥： -url http://localhost:8080 -secret dev-shared-secret
# 发单个心跳：       -device temp-1 -heartbeat
```

查看健康与告警：

```bash
curl -s localhost:8080/api/v1/devices/temp-1/health | jq
curl -s 'localhost:8080/api/v1/alerts?device_id=temp-1' | jq
```

---

## 7. 自动化测试与一键验收

```bash
# 单元 + 集成测试（含 -race 竞态检测）
make test
make test-race

# 端到端验收：真实 HTTP + 真实 HMAC + 真实 SQLite，自动起停服务、中途重启
make acceptance        # 等价于 bash scripts/acceptance.sh
```

验收脚本依次断言：
1. 静止门磁（contact 类型，宽松阈值）**不**被判 frozen；
2. 正常批次（采样 + 心跳）接受、设备健康；
3. 连续 6 个同值、序号推进 → frozen，触发区间 `1003..1008`、带配置版本；
4. 小序号跳变 → gap，触发区间 `1010..1013`；
5. 乱序批量补发填满缺口 → gap 恢复（带恢复区间）；
6. 超大前跳（5000）→ **新 epoch** 且不产生假 gap；
7. 无签名请求 → `401`；
8. 静默超过阈值 → stale；
9. **杀掉并重启服务**，未恢复告警依然存在（状态跨重启保持）。

### 测试覆盖的关键场景
- 采样时间抖动、相同时间戳不误开 epoch；
- 批量乱序补发恢复 gap；
- 进程重启后告警与状态持久保持并继续恢复；
- 相同值但序号正常、且未达类型阈值 → 健康（不冤枉静止设备）；
- 序号缺口进入/补发恢复、重复投递幂等；
- HMAC 篡改体、错误密钥、时间戳越界、nonce 重放。

---

## 8. 目录结构

```
cmd/sensorhealth/        服务入口（含播种）
cmd/ingest-client/       真实 HMAC 签名的上报客户端
internal/domain/         领域模型、可 JSON 序列化的 Duration
internal/config/         默认配置与迟滞约束校验
internal/crypto/         HMAC-SHA256 签名/验签、时间戳与 nonce 防重放
internal/store/          SQLite schema 与数据访问（WAL）
internal/service/        三条规则引擎、epoch/时钟回跳、状态机、持久化
internal/api/            HTTP 路由、鉴权中间件、DTO
examples/                合成输入（健康/冻结/缺口/补发/序列重置）
scripts/acceptance.sh    一键端到端验收
```

## 9. 设计取舍说明
- frozen 仅由**前进采样**推进；历史补发不改变既有的 frozen 判定（补发到达时该判定
  早已基于当时可见样本成立），语义简单可解释。
- 缺口以"连续前锋之后的开放区间"定义，因此先有未达阈值的小洞、后续再跳变时，
  区间始终锚定当前前锋，历史已闭合/未告警的洞不会被重复计入。
