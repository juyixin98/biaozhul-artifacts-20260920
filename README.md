# MQTT 会话重投递 — 本地遥测接收后端

纯后端演示:Go + Mosquitto + PostgreSQL。QoS1 至少一次投递下,用
**业务事件键 (device_id, boot_gen, seq)** 做幂等去重;原消息、业务状态、
去重键在**同一事务**提交,**提交成功才 ACK**。

## 核心概念:传输至少一次 ≠ 业务恰好一次

- **传输层(QoS1)**:broker 保证消息至少送达一次。网络抖动、客户端重连、
  消费者未及时 PUBACK 时,broker 会以 `DUP=1` 重投递。**重复是协议的正常行为**。
- **业务层(去重键)**:同一设备同一代次的同一序号是同一个业务事件。
  `samples` 表上的 `UNIQUE (device_id, boot_gen, seq)` 约束让重复投递
  幂等:第二次插入冲突即跳过,业务效果等价于"恰好一次"。
- **确认时机**:关闭 paho 的自动 ACK(`SetAutoAckDisabled(true)`),
  事务**提交成功后**才 `msg.Ack()`。提交失败则不 ACK——MQTT 3.1.1 没有
  NACK,未确认的 QoS1 消息靠**持久会话(`clean_session=false`)+ 重连**
  由 broker 重投递,这就是"会话重投递"。

## 消息处理规则

| 情况 | 处理 |
|---|---|
| 新业务事件 | 同事务插入 raw_messages + samples + 推进 devices 状态,提交后 ACK |
| 重复投递(业务键冲突) | raw_messages 留痕,samples 冲突跳过,ACK |
| 设备重启(`boot_gen` 更大) | 序号空间按代次隔离,设备状态前进到新代次 |
| 旧代次迟到消息 | 落库并标记 `stale_generation`,**不回退**设备状态 |
| 保留消息重放(`RETAIN=1`) | 落库并标记 `retained_replay`,**不刷新**在线状态 |
| 非法 payload | 同事务写入 raw_messages + quarantine,ACK,不阻塞其他设备 |
| 事务提交失败 | 不 ACK,触发重连,等 broker 会话重投递后重试 |

设备状态推进是单调的:代次只许前进(`GREATEST`),同代次序号取较大者,
乱序/重投不会回退。

## 目录

```
cmd/server/main.go            接入层:MQTT 订阅、手动 ACK、失败重连
internal/telemetry/payload.go 载荷解析与校验
internal/telemetry/store.go   事务核心:raw + 业务 + 去重键同库同事务
internal/telemetry/handler.go 提交后 ACK / 失败后等会话重投递
db/schema.sql                 表结构与业务去重键
scripts/                      建库、起停 broker、跑测试
examples/                     示例 payload 与发布脚本
```

## 本地启动

依赖:Go 1.22+、Mosquitto、PostgreSQL(本机或 Docker 均可)。

```bash
# 1. 数据库:创建角色 mqtt/mqtt_secret、库 mqtt_telemetry 与 mqtt_telemetry_test,并建表
./scripts/setup_db.sh
#    非本机超级用户环境可覆盖:PGADMIN="psql postgres://postgres:pw@host/postgres" ./scripts/setup_db.sh

# 2. broker(若系统 mosquitto 已在 1883 监听可跳过)
./scripts/start_broker.sh

# 3. 接收服务
go build -o var/server ./cmd/server
./var/server
# 环境变量:MQTT_BROKER / MQTT_CLIENT_ID / MQTT_TOPIC / DATABASE_URL
```

## 验收

### 自动化测试(真实 broker + 真实 PostgreSQL)

```bash
./scripts/run_tests.sh
```

集成测试覆盖:

- `TestDuplicateDelivery` — 同一业务事件投递两次,raw 两条、samples 一条
- `TestDeviceReboot` — 重启后新代次序号从 1 重计不冲突;旧代次迟到消息不回退状态
- `TestRetainedMessage` — 保留消息重放不建/不刷新在线状态
- `TestCommitFailureTriggersSessionRedelivery` — 注入 2 次提交失败,
  观察到 `DUP=1` 的协议级重投递,最终只提交一次,失败事务无残留
- `TestQuarantineDoesNotBlockOthers` — 非法 payload 入隔离区,其他设备不受影响

### 手工验收

```bash
./examples/publish_samples.sh   # 发布 6 条各类消息
psql postgres://mqtt:mqtt_secret@127.0.0.1:5432/mqtt_telemetry -c \
  'SELECT device_id,boot_gen,seq,retained_replay,stale_generation FROM samples ORDER BY id;
   TABLE devices;
   SELECT count(*) FROM quarantine;'
```

预期:dev-001 重复投递只记一条;第 2 代 seq=1 与第 1 代 seq=1 并存;
旧代次 seq=2 标记 `stale_generation`;quarantine 1 条。
重启 `./var/server` 后,broker 重放 dev-003 的保留消息,日志出现
`outcome=duplicate retained=true`,而 `devices.updated_at` 不变。

## 依赖锁定

`go.mod` / `go.sum` 已锁定:paho.mqtt.golang v1.4.3、pgx/v5 v5.7.1。
`go build ./...` 即可复现。
