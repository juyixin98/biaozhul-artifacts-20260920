# MQTT 3.1.1 会话子集（Go / 纯标准库）

一个本地、纯后端的 **MQTT 3.1.1 子集 broker**，外加一个基于 `net/http` 的
管理/观测 HTTP API。无界面、无第三方依赖。用 Go 标准库 `net`、`net/http` 实现。

> 语义边界（重要）：本子集提供 **QoS 1 的“至少一次（at-least-once）”投递**。
> 重连/重发时接收端**可能收到重复消息**，需要应用层按报文去重。
> 本子集**不实现 QoS 2，不宣称端到端“恰好一次（exactly-once）”**。

---

## 1. 支持与不支持的报文

### 支持的 MQTT 3.1.1 报文（客户端 → 服务端）

| 报文 | 说明 |
|---|---|
| `CONNECT` | 仅协议名 `MQTT`、协议级别 `4`（3.1.1）；支持 CleanSession、KeepAlive、ClientID |
| `PUBLISH` | 入站 **QoS 0 / QoS 1**；正确处理 `DUP`；出站同样 QoS 0/1 |
| `PUBACK` | 完成一次出站 QoS 1 投递 |
| `SUBSCRIBE` | 支持 `+`、`#` 通配符；授权 QoS = `min(请求, 1)`；非法过滤器回 `0x80` |
| `UNSUBSCRIBE` | |
| `PINGREQ` | 回 `PINGRESP`；按 1.5 × KeepAlive 做服务端保活断连 |
| `DISCONNECT` | CleanSession=1 会话随之删除；持久会话保留状态 |

### 明确不支持（收到后**关闭网络连接**）

- **QoS 2 全套**：`PUBREC` / `PUBREL` / `PUBCOMP`，以及 QoS=2 的 `PUBLISH`。
- **遗嘱（Will）**：`CONNECT` 中 Will 标志置位时，先回 `CONNACK` 返回码 `5`
  （not authorized）再关闭连接，明确告知该特性不在子集内。
- 非 3.1.1 协议（如协议名 `MQIsdp` / 级别 3）：回 `CONNACK` 返回码 `1` 后关闭。
- 任何畸形帧、保留位非法、第二个 `CONNECT`、QoS=3 等协议违例：直接关连接。
- 入站 `PUBLISH` 的 RETAIN 标志被忽略（Retained Message 不在子集内，属已声明行为而非错误）。

---

## 2. 关键语义

- **持久会话（CleanSession=0）**：订阅、未确认的出站消息、离线消息队列、
  下一个 packet ID 全部落到 JSON 快照（`<state>/mqttstate.json`，写临时文件 +
  fsync + 原子 rename）。broker 重启后，同一 ClientID 以 clean=false 重连时
  `CONNACK` 的 Session Present=1。
- **packet ID 按会话、按方向独立管理**：broker→每个客户端方向各自从 1 起分配，
  互不影响。因此两个不同会话收到同一条消息时可以都使用 packet ID 1。
- **确认丢失与重发**：出站 QoS 1 在 PUBACK 到达前记为 inflight。连接断开且未收到
  PUBACK 时，重连后以**相同 packet ID、`DUP=1`** 重发（MQTT 3.1.1 §3.3.1.1 / §4.4）。
- **离线队列**：客户端离线期间，QoS 1 消息排队；重连后作为**全新投递**（`DUP=0`、
  新 packet ID）发出。QoS 0 不对离线客户端排队（§3.1.2-5）。
- **QoS 降级**：投递 QoS = `min(发布 QoS, 订阅授权 QoS)`（§4.3）。QoS 1 发布到
  QoS 0 订阅按 QoS 0 投递、不跟踪重发。
- **重复入站**：发布端重发的 QoS 1（`DUP=1`、相同 packet ID）会被再次 PUBACK
  并再次投递给订阅者——这正是“至少一次”，订阅端需自行去重。
- **同 ClientID 接管**：新连接踢掉旧连接（§3.1.4-2）。

持久化顺序采用“先更新并落盘投递状态，再写网络”，因此崩溃只可能导致重复，
不会丢失已接受的 QoS 1 消息。

---

## 3. 目录结构

```
.
├── go.mod                     # 模块定义；无 require，零第三方依赖
├── cmd/
│   ├── mqttserver/main.go     # 服务入口：MQTT TCP + HTTP API
│   └── mqttdemo/main.go       # 极简可脚本化 MQTT 客户端（演示/手测）
├── mqtt/                      # MQTT 子集协议、broker、持久化
│   ├── codec.go               # 报文编解码、主题匹配
│   ├── broker.go              # 会话、投递、packet ID 管理
│   ├── server.go              # TCP 连接生命周期、CONNECT/保活/接管
│   ├── store.go               # JSON 快照持久化
│   ├── *_test.go              # 协议单测 + 端到端集成测试
└── httpapi/
    ├── handler.go             # net/http 管理接口
    └── handler_test.go
```

---

## 4. 依赖与启动

### 依赖

- Go（开发与实测版本 **go1.23.1 linux/amd64**；`go.mod` 声明 `go 1.23`）。
- **仅使用 Go 标准库**，`go.mod` 没有任何 `require` 条目，因此不需要
  `go.sum`（这就是本项目“锁定依赖”的方式：工具链版本在 `go.mod` 中固定，
  外部依赖集合为空）。
- 网络：默认 MQTT TCP `:1883`，HTTP `:8080`。

### 构建

```bash
go build ./...
go build -o bin/mqttserver ./cmd/mqttserver
go build -o bin/mqttdemo   ./cmd/mqttdemo
```

### 启动服务

```bash
go run ./cmd/mqttserver -mqtt :1883 -http :8080 -state ./state
# 或
bin/mqttserver -mqtt 127.0.0.1:1883 -http 127.0.0.1:8080 -state ./state
```

参数：

- `-mqtt`：MQTT TCP 监听地址（默认 `:1883`）
- `-http`：HTTP API 监听地址（默认 `:8080`）
- `-state`：持久化快照目录（默认 `./state`，不存在会创建）

---

## 5. HTTP 接口与请求样例

| 方法与路径 | 作用 |
|---|---|
| `GET  /healthz` | 存活探针 |
| `GET  /v1/sessions` | 列出全部会话（订阅、inflight/排队计数、下一个 packet ID、在线状态） |
| `GET  /v1/sessions/{clientID}` | 查看单个会话 |
| `DELETE /v1/sessions/{clientID}` | 删除会话（踢掉在线连接并清除持久状态） |
| `POST /v1/publish` | 以 broker 身份注入一条消息（走相同的投递/QoS/持久化规则） |

`POST /v1/publish` 请求体：

```json
{ "topic": "a/b", "qos": 1, "payload_text": "hello" }
```

或二进制载荷：

```json
{ "topic": "a/b", "qos": 1, "payload_base64": "aGVsbG8=" }
```

curl 样例：

```bash
curl -s http://127.0.0.1:8080/healthz
curl -s http://127.0.0.1:8080/v1/sessions
curl -s http://127.0.0.1:8080/v1/sessions/subA

curl -s -X POST http://127.0.0.1:8080/v1/publish \
  -H 'Content-Type: application/json' \
  -d '{"topic":"evt/1","qos":1,"payload_text":"first-delivery"}'

curl -s -X DELETE http://127.0.0.1:8080/v1/sessions/subA
```

`POST /v1/publish` 成功响应（202）会显式提示语义边界：

```json
{
  "topic": "evt/1",
  "qos": 1,
  "matched_subscribers": 1,
  "delivered": 1,
  "note": "at-least-once: QoS 1 messages are retried until PUBACK; no end-to-end exactly-once guarantee"
}
```

> HTTP 端口是管理/观测面，**不是** MQTT 通道；MQTT 报文走原始 TCP。

---

## 6. 验收场景演示（mqttdemo）

`cmd/mqttdemo` 是自带的极简 MQTT 客户端，便于手工跑通验收项。

```bash
# 终端 A：持久订阅者；-noack = 永远不回 PUBACK，模拟“确认丢失”
bin/mqttdemo sub   -addr 127.0.0.1:1883 -id subA -filter "evt/#" -qos 1 -noack

# 终端 B：QoS 1 发布
bin/mqttdemo pub   -addr 127.0.0.1:1883 -id pubA -topic evt/1 -qos 1 -pid 4242 -msg "first-delivery"
```

A 第一次看到 `PUBLISH dup=false qos=1 id=1 ...`。随后让 A 进程退出
（模拟断连且 PUBACK 丢失），再以相同 `-id subA` 重连：

```bash
bin/mqttdemo sub -addr 127.0.0.1:1883 -id subA -filter "evt/#" -qos 1 -noack
```

重连时 `connected; session-present=true`，并收到
`PUBLISH dup=true qos=1 id=1 ... payload="first-delivery"` —— **相同 packet ID、
DUP=1 的重发**。broker 中途重启（`kill` 后用同一 `-state` 目录再起）也会从磁盘
恢复并重发。

相同 ID、不同会话：

```bash
bin/mqttdemo sub -id sessA -filter shared -qos 1 -noack &
bin/mqttdemo sub -id sessB -filter shared -qos 1 -noack &
curl -s -X POST http://127.0.0.1:8080/v1/publish \
  -d '{"topic":"shared","qos":1,"payload_text":"same-pid"}'
# sessA 与 sessB 各自收到 id=1（packet ID 命名空间相互独立，各自 inflight 独立跟踪）
```

其它命令：`bin/mqttdemo pub -clean ...`（CleanSession=1）、
`bin/mqttdemo ping -id pinger -keepalive 10`。

---

## 7. 自动化测试

```bash
go test ./...            # 全部测试
go test -race ./...      # 竞态检测
go test -cover ./...     # 覆盖率
go test -v ./mqtt/       # 详细用例
```

测试包含一个直接读写原始 TCP 字节的 MQTT 客户端夹具，覆盖：

- CONNECT/CONNACK、Session Present、SUBSCRIBE/SUBACK、UNSUBSCRIBE、PING、通配符；
- **确认丢失 → 重连重发（DUP=1、相同 packet ID）**；
- **broker 重启后从磁盘恢复并重发**；
- **相同 packet ID、不同会话彼此独立**；
- 入站 DUP=1 重复（订阅端收到两份，需去重——至少一次）；
- 离线队列以全新（DUP=0、新 ID）投递；
- QoS2 PUBLISH / PUBREL / Will / 旧协议 → 关闭连接；
- KeepAlive 1.5 倍超时断连；同 ClientID 接管踢掉旧连接；
- HTTP API：健康检查、发布、会话查询、删除、参数校验。

---

## 8. 实测结果（如实记录）

在 `go1.23.1 linux/amd64` 上实际运行：

- `go vet ./...`：通过。
- `go test -race -count=3 ./...`：**全部通过**（重复 3 轮，含竞态检测）。
  包 `mqtt` 与 `httpapi` 均 `ok`；语句覆盖率约 **77%**（`mqtt` 包）。
- 使用真实二进制端到端跑通并人工核对了：
  - 确认丢失后重连：`dup=false id=1` → 断连 → 重连 `dup=true id=1`；
  - broker 重启后从 `mqttstate.json` 恢复并 `dup=true id=1` 重发；
  - 两个会话各自独立的 `id=1`，inflight 分别计数；
  - 离线消息重连后以 `dup=false id=2/id=3` 全新投递；
  - Will 回 `CONNACK 20 02 00 05` 后关闭；QoS2 PUBLISH 与 PUBREL 直接 EOF 关连接；
  - `-clean` 会话在 broker 重启后不残留。

### 已知限制 / 未完成项（不夸大）

- **无 QoS 2、无 Retained Message、无 Will、无 AUTH/认证授权**（用户名/密码字段
  可解析但不校验）。这些都是显式不支持，不是疏漏。
- 只保证**至少一次**：重复投递不做服务端去重，端到端恰好一次不在范围内。
- 持久化用单个 JSON 快照、每次状态变更整体重写，面向本地/单机轻负载；未做
  分片、WAL 或高可用，未做集群桥接。
- 离线队列上限 10000 条/会话（满了丢最旧），单包应用层上限 1 MiB。
- 未实现 TLS（MQTT over TLS/8883）与 WebSocket。
- 未提供 $SYS 主题；可观测性主要通过 HTTP API。
