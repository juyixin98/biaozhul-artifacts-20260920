# mqttsub — MQTT 3.1.1 会话子集（Go / net/http）

一个用 **Go 标准库**（`net`、`net/http`，零第三方依赖）实现的本地 MQTT 3.1.1
子集 Broker，纯后端、无界面。MQTT 协议跑在原生 TCP 上；旁边提供一个
`net/http` 控制接口用于注入消息和查看会话，便于演示与自动化测试。

> **投递语义：至少一次（at-least-once）。** QoS 1 消息在接收方会话收到
> `PUBACK` 前一直保存在 inflight 中；ACK 丢失、连接断开或进程重启后会以
> `DUP=1`、**相同 packet id** 重发。因此重复投递是设计使然，需要消费方按
> 应用语义去重。本程序**不声称端到端恰好一次（exactly-once）**——那需要
> QoS 2 四步握手与持久化 ACK 协调，本子集明确不实现。

---

## 1. 功能范围

### 支持
- `CONNECT` / `CONNACK`：协议名校验（必须 `MQTT`）、协议级别 4、clean session、
  keep-alive（按 1.5 倍超时断连）、遗嘱（Will，QoS0/1）、username/password 字段解析。
- 会话 Present 标志：clean=false 且已有持久会话时 CONNACK 返回 `sessionPresent=1`。
- `SUBSCRIBE` / `SUBACK`、`UNSUBSCRIBE` / `UNSUBACK`；过滤器支持 `+`、`#`
  通配符（含 §4.7.2 “`sport/#` 不匹配 `sport`” 规则）。
- `PUBLISH`：**QoS 0 与 QoS 1**；订阅 QoS 低于发布 QoS 时按授予 QoS 降级投递。
- `PUBACK`：出站 QoS1 的确认；inflight 清除。
- **packet id 按（接收方）会话独立管理**，不同会话可同时使用相同 id。
- **持久会话**（clean=false）：订阅、离线消息队列、未确认 inflight、入站已见 id、
  packet-id 游标均落盘（单个 JSON 文件，临时文件 + rename 原子改写，防抖批量写）。
- 重连重放：先按发送顺序重发 inflight（`DUP=1`），再排空离线队列。
- 在线重传定时器：连接保持但 ACK 迟迟不来时，周期性重发 inflight（`DUP=1`）。
- 入站去重：收到已应答过的相同 packet id（`DUP=1`）时**重发 PUBACK 但不再向
  订阅者扇出**。
- `PINGREQ` / `PINGRESP`、`DISCONNECT`。
- 同一 client id 再次连接会**踢掉旧连接**（接管不触发遗嘱，§3.1.4-3）。
- 异常断连（无 DISCONNECT）发布遗嘱；正常 DISCONNECT 不发布。

### 明确不支持（收到即视为协议违规并**直接关闭连接**，不回任何报文，§4.8）
- **QoS 2 全套**：`PUBREC`(5) / `PUBREL`(6) / `PUBCOMP`(7)。
  - 订阅时请求 QoS2 不报错，而是**授予 QoS1**（SUBACK 返回 1）。
  - 发布 QoS2、收到上述任何报文 → 关闭连接。
- 保留报文类型 0、15；在已建立的连接上收到第二个 `CONNECT`。
- Retained Message（PUBLISH 的 retain 位被接受但无效）。
- 共享订阅、`$` 系统主题、TLS、WebSocket、AUTH（MQTT5）。
- 不做用户名/密码鉴权决策（字段解析但一律接受）。

---

## 2. 目录结构

```
.
├── go.mod                       # 模块声明；零 require，依赖即被锁定
├── internal/
│   ├── mqtt/                    # MQTT 3.1.1 报文编解码子集 + 主题匹配
│   │   ├── codec.go
│   │   ├── topic.go
│   │   └── *_test.go
│   ├── broker/                  # 会话/订阅/QoS1/ACK/DUP/持久化
│   │   ├── broker.go            # 核心状态与路由
│   │   ├── conn.go              # 单连接协议状态机
│   │   ├── pump.go              # inflight 重放 + pending 排空 + 在线重传
│   │   ├── persist.go           # 防抖快照循环
│   │   ├── store.go             # JSON 原子落盘
│   │   ├── admin.go lifecycle.go
│   │   └── *_test.go            # 端到端验收测试（真实 TCP）
│   └── httpapi/                 # net/http 控制接口 + 测试
└── cmd/
    ├── mqttd/main.go            # 服务端：MQTT TCP + HTTP API
    └── mqttcli/                 # 演示客户端：pub / sub / lossdemo
```

---

## 3. 依赖与启动

### 依赖
- Go **1.23+**（开发机实测 go1.23.4 linux/amd64）。
- **仅用 Go 标准库**，`go.mod` 无任何 `require`，因此没有也不需要 `go.sum`
  条目——依赖已被锁定为“标准库版本 + go.mod 中的 go 指令”。

### 构建

```bash
go build ./...
go vet ./...
gofmt -l .      # 应无输出
```

### 启动服务端

```bash
go run ./cmd/mqttd \
  -mqtt  127.0.0.1:1883  \
  -http  127.0.0.1:8081  \
  -store ./mqtt-sessions.json \
  -retry 10s
```

参数：
| 参数 | 默认 | 含义 |
|---|---|---|
| `-mqtt` | `:1883` | MQTT TCP 监听地址 |
| `-http` | `:8081` | HTTP 控制 API 监听地址 |
| `-store` | `mqtt-sessions.json` | 持久会话文件；传空串 `""` 关闭持久化 |
| `-retry` | `10s` | 在线 inflight 重传间隔；`0` 表示仅在重连时重传 |

`Ctrl+C`（SIGTERM/SIGINT）会关闭监听与连接，并**同步落盘最后一次快照**。

---

## 4. HTTP 控制接口（net/http）与请求样例

```bash
# 健康检查
curl -s 127.0.0.1:8081/healthz
# {"status":"ok"}

# 计数概览
curl -s 127.0.0.1:8081/stats

# 注入一条消息（QoS1，payload 为 JSON 字符串；也支持 {"base64":"..."}）
curl -s -XPOST 127.0.0.1:8081/publish \
  -H 'Content-Type: application/json' \
  -d '{"topic":"sensors/1","qos":1,"payload":"42"}'
# 202 -> {"topic":"sensors/1","qos":1,"bytes":2,"matched_sessions":N}

# QoS0
curl -s -XPOST 127.0.0.1:8081/publish \
  -d '{"topic":"sensors/1","qos":0,"payload":"fire-and-forget"}'

# 非法输入返回 400
curl -s -XPOST 127.0.0.1:8081/publish -d '{"topic":"a/+","qos":1}'   # 主题不能含通配符
curl -s -XPOST 127.0.0.1:8081/publish -d '{"topic":"a","qos":2}'     # QoS2 不支持

# 列出所有会话 / 查看单个 / 删除（清理持久会话）
curl -s 127.0.0.1:8081/sessions
curl -s 127.0.0.1:8081/sessions/<clientID>
curl -s -XDELETE 127.0.0.1:8081/sessions/<clientID>
```

`GET /sessions/<id>` 示例（`inflight` 是未确认 packet id 列表，
`next_packet_id` 显示该会话独立的 id 游标）：

```json
{
  "client_id": "dur1",
  "durable": true,
  "online": false,
  "subscriptions": {"demo/#": 1},
  "pending": 1,
  "inflight": [1],
  "next_packet_id": 2
}
```

---

## 5. 验收场景与实测结果

### 5.1 自动化测试

```bash
go test -race ./...
```

实测（go1.23.4，`-race`，多次重复）**全部通过**：

```
ok  	mqttsub/internal/broker    2.7s
ok  	mqttsub/internal/httpapi   1.0s
ok  	mqttsub/internal/mqtt      1.0s
```

覆盖的关键用例（真实 TCP 端到端）：
- `TestAckLossReconnectDup`：**确认丢失 → 崩溃 → 重连 → DUP=1 重发、相同 pid**。
- `TestSamePacketIDDifferentSessions`：**相同 packet id 用于不同会话**，且
  ACK 互不串扰。
- `TestOfflineDurableQueue`：离线期间 QoS1 入队，重连按 FIFO、DUP=0、新 id 投递。
- `TestInboundDuplicateNotRefanned`：入站 DUP 重发只回 PUBACK、不重复扇出。
- `TestRestartPersistence`：**整个 broker 重启后** inflight 恢复，重连收到 DUP=1。
- `TestUnsupportedPacketsClose` / `TestFirstPacketNotConnect`：QoS2 报文、
  非 CONNECT 首包 → 立即关闭连接。
- `TestWillMessage`、`TestTakeoverClosesOldWithoutWill`、`TestLiveRetryDup`、
  `TestQoSDowngrade`、`TestSubscribeQoS2Downgrade`、`TestPing` 等。

### 5.2 一键验收脚本（真实进程）

终端 A 启动服务，终端 B 运行编排好的两个验收场景：

```bash
go run ./cmd/mqttd -mqtt 127.0.0.1:18830 -http 127.0.0.1:18081 \
  -store /tmp/s.json -retry 8s

go run ./cmd/mqttcli lossdemo -addr 127.0.0.1:18830 -http http://127.0.0.1:18081
```

本机实测输出（节选）：

```
== scenario 1: PUBACK lost -> reconnect -> DUP=1 redelivery ==
A: got #1 DUP=false pid=1 payload="message-1"  (PUBACK deliberately withheld)
A: TCP dropped without PUBACK/DISCONNECT (simulated crash)
A: reconnected, CONNACK sessionPresent=true
A: got #2 DUP=true pid=1 payload="message-1"
A: PUBACK sent for pid=1
A: PASS — at-least-once redelivery with DUP=1 and stable packet id

== scenario 2: same packet id, independent sessions ==
X: DUP=false pid=1 payload="message-2"
Y: DUP=false pid=1 payload="message-2"
PASS — packet id 1 used concurrently by two different sessions

ALL SCENARIOS PASSED
note: guarantee is at-least-once — duplicates after PUBACK loss ...
```

### 5.3 跨进程重启持久化（手工实测）

1. 持久客户端 `dur1` 订阅 `demo/#`，收到 `demo/a`(pid=1) 后**扣留 PUBACK 并硬退出**；
2. 其离线期间经 HTTP 发布 `demo/b`（`matched_sessions=1`，进入离线队列）；
3. SIGTERM 停止 broker，落盘 JSON 同时含 `inflight(pid=1, demo/a)` 与
   `pending(demo/b)`；
4. **重启 broker 进程**，同 client id 以 clean=false 重连，实测依次收到：

```
RECV DUP=true  QoS=1 pid=1 topic=demo/a payload="before-restart"
RECV DUP=false QoS=1 pid=2 topic=demo/b payload="while-offline"
```

随后 inflight / pending 清空，`next_packet_id=3`。

### 5.4 手工 pub/sub

```bash
# 订阅者（默认 clean=false 持久会话；-hold 可扣留 PUBACK）
go run ./cmd/mqttcli sub -addr 127.0.0.1:1883 -id sub-1 -filter "sensors/+" -duration 30s

# 发布者
go run ./cmd/mqttcli pub -addr 127.0.0.1:1883 -id pub-1 -topic sensors/1 -msg 42
```

---

## 6. 设计要点

- **单把 Broker 锁**保护会话表与会话状态；网络写在锁外（每连接 pump 串行化
  出站），慢消费者不会阻塞路由。
- packet id 在会话内 `1..65535` 循环分配，跳过仍在 inflight 的 id；全忙时
  出队暂停等待 ACK。
- 出队后若写失败：QoS1 已在 inflight（天然不丢），QoS0 在 cleanup 中回卷到
  pending 队首；持久会话离线队列得以保留。
- 持久化用防抖（200ms）合并突发写，关闭时再同步刷一次；写盘走
  `tmp + rename`，避免半截文件。

---

## 7. 未完成项 / 已知限制（如实说明）

- **不是完整 MQTT 3.1.1 实现**：无 QoS2、无 Retained、无 TLS/WS、无鉴权、
  无 `$` 系统主题、无共享订阅。
- 持久化是“进程优雅退出 / 防抖落盘”级别；写入后未 `fsync`，机器掉电可能丢失
  最近 200ms 窗口（崩溃前的 inflight 因重发机制通常仍可恢复）。
- 订阅 QoS2 采用**降级授予 QoS1**而非 SUBACK 失败 0x80 的策略。
- 存储为单个 JSON 文档、全内存会话表，面向本地/演示规模，未做分片或大消息治理。
- HTTP `/publish` 只做服务端侧注入，它不是 MQTT 客户端；消息的 QoS1 可靠性
  作用于“broker → MQTT 订阅者”这一段。
- 遗嘱/离线消息只对 clean=false 持久会话排队；clean 会话断连即销毁（符合规范）。
