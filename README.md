# MQTT 会话重投递 —— 本地遥测接收后端

纯后端实现：设备经 MQTT QoS 1 上报遥测，后端在 **一个 PostgreSQL 事务**里原子写入
「原消息 + 业务状态 + 去重键」，**事务提交成功之后才回 PUBACK**。
设备身份由 `(device_id, boot_gen, seq)` 唯一确定——重连、设备重启都不会混淆序号。

技术栈：**Go 1.22**（paho.golang MQTT v5 手动ACK + pgx/v5）、**Mosquitto 2.0**（Docker）、**PostgreSQL 16**（Docker）。

---

## 1. 传输「至少一次」与业务「去重」的区别（本项目核心）

这两件事处在不同层，**不能相互替代**：

| | 传输层（MQTT QoS 1） | 业务层（本服务的去重） |
|---|---|---|
| 保证什么 | 消息**至少**送达一次 | 同一业务事件**最多**生效一次 |
| 靠什么 | PUBACK/重发、DUP 标志、持久会话 | 业务主键 `(device_id, boot_gen, seq)` |
| 允许重复吗 | **允许且必然会发生**（这正是 QoS1 的定义） | 不允许：重复交付只产生审计行，不改业务状态 |
| 标识 | MQTT packet id（重发时可能变化）、DUP 位 | 设备ID + 启动代次 + 序号（与传输无关） |

一条消息在两种情况下会被收到多次：

1. **业务事务提交失败/未提交**（PUBACK 没发出）→ broker 重投递。
   重投递时事件首次插入，业务行恰好 1 条。这是「至少一次」在补做工作。
2. **事务已提交，但 PUBACK 丢失/进程在提交后崩溃** → broker 仍会重投递。
   重投递命中业务主键 → 走 `dup` 分支，**业务行仍是 1 条**，只多一条 `raw_messages.kind='dup'` 审计行。
   这是「业务去重」在兜底。

因此正确性依赖一条铁律：

> **先提交数据库事务，成功后再发 PUBACK。绝不先 ACK。**

代码位置：`internal/broker/consumer.go`（`handleOne`/`process`）、
`internal/store/process.go`（`ProcessEvent`，唯一约束 + 同事务写原消息与业务状态）。

### 为什么业务键是 (设备ID, 启动代次, 序号) 而不是只用序号？

设备重启后本地序号会从 1 重新开始。若只用 `(device_id, seq)`，重启后的 `seq=1`
会被误判成「第一条的重复」而丢弃，或与旧数据混淆。加入单调递增的 `boot_gen`：

- 同一 `(device, boot)` 内 `seq` 单调，负责检测重连/重发；
- `boot` 不同则是不同的序号空间，重启后的样本被正确当成新事件；
- 在线状态 `device_state` 只跟随**最新代次**（按首次出现时间 `device_boots.first_seen` 判定），
  旧代次的迟到包照常入库，但**不会把 current_boot/current_seq 回退**。

---

## 2. 消息协议

- Topic：`telemetry/<deviceID>`，QoS 1（发布端可置 RETAIN）。
- Payload：UTF-8 JSON：

```json
{
  "device_id": "dev-001",
  "boot_gen": 1,
  "seq": 1,
  "value": 213.7,
  "ts_ms": 1727100000123,
  "sig": "<64位小写hex HMAC-SHA256>"
}
```

- `sig` 是对以下**规范化字节串**做 HMAC-SHA256（密钥为设备预置密钥），真正用
  `crypto/hmac`+`crypto/sha256` 计算并常量时间比较（`hmac.Equal`），无任何旁路：

```
v1
device=dev-001
boot=1
seq=1
value=213.7
ts=1727100000123
```

示例文件中的签名由 `go run ./examples/gen` 真实计算生成，可重新生成核对。

---

## 3. 各类消息的处理结果

| 输入 | 处理 | 是否 ACK | 影响在线状态 |
|---|---|---|---|
| 合法新事件 | `events` 插入 + 更新 boot/state + 原消息 `kind=event` | 提交后 ACK | 刷新 |
| 合法事件的重复投递 | 主键冲突 → 仅写原消息 `kind=dup` | 提交后 ACK | **不刷新** |
| **RETAINED** 消息（**建立新订阅时** broker 回放的快照） | 仅写原消息 `kind=snapshot` | 提交后 ACK | **绝不刷新、不产生 event** |
| 非法 JSON / 字段非法 / topic 不符 | 写 `quarantine` + 原消息 `kind=quarantine` | 提交后 ACK | 无 |
| 设备未注册 | 隔离区，原因 `unregistered device_id` | 提交后 ACK | 无 |
| HMAC 签名错误 | 隔离区，原因 `signature mismatch` | 提交后 ACK | 无 |
| 合法事件但**业务事务回滚** | 整事务回滚，什么都不留 | **不 ACK**，断连触发重投递 | 无 |
| 合法事件已提交但 **ACK 丢失**（注入） | 已生效；重投递命中去重 | 重投递时 ACK | 首次已刷新 |

非法 payload 按**每设备一条串行 worker** 处理：坏设备既不会因坏消息阻塞
（坏消息立即隔离+ACK），也不会因自身合法消息持续失败而拖累其他设备
（只影响自己的队列和连接重连周期）。

### 关于 RETAINED 的一个 MQTT 语义要点

按 MQTT 规范，向**当前在线**的订阅者发布一条 RETAINED 消息时，broker 当下
按普通消息（Retain 位=0）投递它；Retain 位=1 的快照只在**建立新订阅**时
由 broker 重放，且不会在持久会话重连时反复下发。因此要演示/验收「保留快照
不刷在线状态」，正确姿势是：**先在没有任何订阅者时发布 retained，再启动
consumer 建立全新订阅**（`scripts/acceptance.sh` 场景1 即如此）。订阅端设置
`RetainHandling=1`，确保快照只在首次建立订阅时接收一次。

---

## 4. 目录结构

```
cmd/consumer   后端服务：订阅、校验、验签、事务提交、提交后ACK、断线重连
cmd/device     设备模拟器：真实HMAC签名，按 boot/seq 编号，QoS1 发布
cmd/inject     注入坏JSON/坏签名/未注册设备/保留消息/任意(boot,seq)
cmd/seed       预置设备 HMAC 密钥
examples/gen   用真实密码学生成 examples/ 下带签名的样例
internal/crypto    HMAC-SHA256 签名/验签（真实密码学）
internal/telemetry 协议解析与永久/临时错误分类
internal/store     事务边界、去重、在线状态、快照、隔离区、schema(embed)
internal/broker    MQTT v5 手动ACK、每设备队列、断线重连、故障注入
internal/broker/admin  JSON 运维接口（无前端）
deploy/mosquitto.conf  broker 配置（持久化）
scripts/acceptance.sh  端到端验收（自起容器，7类场景）
```

---

## 5. 本地启动

需要：Go 1.22+、Docker（用于 Mosquitto/PostgreSQL）。

```bash
make up                 # 起 Mosquitto:11883 与 PostgreSQL:55433（避开系统默认端口）
make build
make seed               # 预置 dev-001 / dev-002（默认密钥 secret-<id>）
make run                # 启动 consumer（管理接口 127.0.0.1:8079）
```

另开终端：

```bash
make sample             # dev-001 boot=1 发 5 条
make state              # 查看在线状态/事件数/隔离区数
make metrics            # 接收/入库/去重/快照/隔离/失败/重连计数
```

不用 make 的等价命令见下文「手动验收命令」。

管理接口（纯 JSON）：

```
GET  /healthz
GET  /metrics
GET  /state
GET  /faults
POST /faults/arm?device=ID      # 让该设备业务事务强制回滚
POST /faults/dropack?device=ID  # 一次性：提交成功但丢弃ACK
POST /faults/clear?device=ID    # 清除（device=* 或省略=清全部）
```

---

## 6. 验收

### 一键端到端（推荐）

```bash
make acceptance         # 或 bash scripts/acceptance.sh
```

脚本会自起全新容器并依次真实验证：正常QoS1、设备重启序号不混淆、
非法payload隔离不阻塞他人、保留消息不刷在线状态、业务提交失败回滚+重投递恰好一次、
提交后ACK丢失触发业务去重、consumer `kill -9` 崩溃后持久会话重投递。

### Go 测试

```bash
make test               # 纯单元测试：HMAC验签/防篡改、协议校验、错误分类
make test-all           # 需先 make up：额外跑 PostgreSQL 集成测试
                        # （事务去重、重启不混淆、回滚不留痕、快照/隔离区）
```

---

## 7. 手动验收命令（逐步，便于观察）

终端A：

```bash
docker compose up -d
go build -o bin/ ./cmd/...
./bin/seed -id dev-001
./bin/seed -id dev-002
./bin/consumer
```

终端B：

```bash
# (1) 正常：5条入库，在线 seq=5
./bin/device -id dev-001 -boot 1 -count 5
curl -s localhost:8079/state

# (2) 设备重启：boot 1->2，seq 重新从1；状态前进到 boot=2/seq=3，与旧序号不混淆
./bin/device -id dev-001 -boot 2 -count 3
curl -s localhost:8079/state

# (3) 坏设备不阻塞：dev-002 发坏JSON+坏签名（进隔离区并ACK），同时 dev-001 正常
./bin/inject badjson dev-002
./bin/inject badsig  dev-002
./bin/device -id dev-001 -boot 2 -seq-start 4 -count 1
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -c \
  "SELECT reason FROM quarantine ORDER BY id;"

# (4) 保留消息不是新采样：必须在 consumer 没有订阅时发布，再启动 consumer。
#     首次订阅会收到 broker 回放的快照(Retain=1)：记 kind=snapshot，
#     events 不增加、device_state 不建立/不刷新。
./bin/inject retained dev-001      # consumer 未运行时执行
./bin/consumer                     # 再启动；日志见 "RETAINED snapshot ... state untouched"
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -c \
  "SELECT kind, retained_flag FROM raw_messages WHERE kind='snapshot';"

# (5) 业务提交失败 -> 回滚不ACK -> broker重投递 -> 清故障后恰好1条业务行
curl -s -X POST "localhost:8079/faults/arm?device=dev-001"
./bin/device -id dev-001 -boot 2 -seq-start 5 -count 1
# 观察 consumer 日志: NO-ACK ... forcing reconnect，反复重投递
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -tAc \
  "SELECT count(*) FROM events WHERE device_id='dev-001' AND boot_gen=2 AND seq=5;"  # 0
curl -s -X POST "localhost:8079/faults/clear"
# 等待重连重投递后再查：恰好 1
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -tAc \
  "SELECT count(*) FROM events WHERE device_id='dev-001' AND boot_gen=2 AND seq=5;"  # 1

# (6) 提交成功但ACK丢失：业务行=1，重投递产生 kind='dup' 审计行
curl -s -X POST "localhost:8079/faults/dropack?device=dev-001"
./bin/device -id dev-001 -boot 2 -seq-start 6 -count 1
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -c \
  "SELECT id, kind, dup_flag, payload->>'seq' AS seq FROM raw_messages WHERE device_id='dev-001' ORDER BY id DESC LIMIT 4;"
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -tAc \
  "SELECT count(*) FROM events WHERE device_id='dev-001' AND boot_gen=2 AND seq=6;"   # 1
docker exec mqttredel-pg psql -U mqttredel -d mqttredel -tAc \
  "SELECT count(*) FROM raw_messages WHERE device_id='dev-001' AND kind='dup';"      # >=1

# (7) 崩溃重启：arm 故障 -> 发1条 -> kill -9 consumer -> 重启(sessionPresent=true)
#     -> 清故障，未ACK消息由持久会话重投，业务行恰好1
```

> 重置数据重来：
> `docker exec mqttredel-pg psql -U mqttredel -d mqttredel -c "TRUNCATE raw_messages, events, device_boots, device_state, quarantine RESTART IDENTITY;"`
> 彻底清环境：`make clean`。

---

## 8. 设计要点与故障语义备注

- **手动 ACK**：paho v5 `EnableManualAcknowledgment`，ACK 由内部 tracker 按 packet id
  顺序发出；我们只在事务提交成功后调用 `client.Ack(pb)`。
- **持久会话**：consumer 固定 client id、`CleanStart=false`、
  `SessionExpiryInterval=7200`；未 ACK 的 QoS1 由 Mosquitto 持久保存，
  consumer 重启后 CONNACK 的 `sessionPresent=true` 并重投。
- **重投递触发方式**：合法消息事务失败时不回 PUBACK，并主动断开 TCP（不发 DISCONNECT），
  监管协程按退避重连（故障未清除前不立即重连，避免空转）；broker 随后以 DUP=1 重发。
- **原消息留存**：`raw_messages.payload_raw` 保存原始字节，`payload` 保存解析后的 JSONB，
  与业务状态同事务；回滚则三者皆不留。
- **在线状态**：只有合法新事件刷新；重复投递、RETAINED 快照、旧代次迟到包均不刷新/回退。
- **依赖锁定**：见 `go.mod` / `go.sum`（paho.golang v0.21.0、pgx/v5 v5.7.1 等）。
