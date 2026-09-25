# mqtt-subset

MQTT 3.1.1 **协议子集**的纯后端实现（Rust，零外部依赖）：增量字节解析库 +
本地 TCP 测试服务器。核心解析与状态机全部手写，未借用任何现成 MQTT 协议库。

## 支持的子集（明确边界）

| 报文 | 支持情况 |
|---|---|
| CONNECT / CONNACK | ✅（clean session、空客户端 ID 分配、will 解析与异常断连投递；username/password 仅解析不鉴权） |
| SUBSCRIBE / SUBACK | ✅（多主题过滤器；请求 QoS2 降级授予 QoS1） |
| PUBLISH（QoS0 / QoS1）/ PUBACK | ✅ |
| PINGREQ / PINGRESP、DISCONNECT | ✅ |
| QoS2（PUBREC/PUBREL/PUBCOMP） | ❌ 收到即报错并关闭连接 |
| UNSUBSCRIBE | ❌ 收到即报错并关闭连接 |
| MQTT 5、MQTT 3.1、WebSocket、TLS、鉴权 | ❌ |

主题过滤器支持 `+` 与 `#` 通配符；`$` 开头的系统主题不被通配符匹配（§4.7.2）。

## 投递语义（重要声明）

本实现承诺 **至少一次（at-least-once）**，**不宣称业务恰好一次**：

- 服务器→客户端：QoS1 消息在收到 PUBACK 前保存在 `inflight_out`；
  持久会话（clean_session=false）重连时以 **DUP=1 重发**。
  订阅方因此**可能收到重复消息**，去重是业务层的责任。
- 客户端→服务器：服务器按（会话, 包ID）维护去重窗口，仅对 **DUP=1**
  的重发去重（PUBACK 丢失后客户端重发的场景），并重新应答 PUBACK。
  DUP=0 一律视为新消息——包 ID 在 PUBACK 完成后允许立即复用。
- 重发时机：仅在会话恢复（重连）时重发，**不做定时器重传**（已知限制）。
- 离线持久会话：QoS1 消息排队，重连后补投；QoS0 丢弃（符合规范）。

## 长度上限与错误类型

- 默认单包上限 64 KiB（`DEFAULT_MAX_PACKET_SIZE`），服务器可用
  `--max-packet` 调整；协议硬上限 268435455 字节。超限在读取声明长度后、
  分配缓冲前即拒绝。
- 错误类型（`src/error.rs` 的 `MqttError`）：
  `Malformed`（格式/标志位非法）、`PacketTooLarge{max,got}`、
  `UnsupportedPacketType(u8)`、`UnsupportedQos(u8)`、
  `UnsupportedProtocol{name,level}`、`ProtocolViolation(&'static str)`、`Io`。
  服务器对任何解析/协议错误的处理是关闭连接（MQTT 3.1.1 的规定做法）。

## 结构

```
src/
  codec.rs    增量字节解析器 Decoder（feed/next_packet）+ 编码器 encode
  packet.rs   报文类型（Connect/Publish/Subscribe/...）
  error.rs    MqttError 错误枚举
  topic.rs    主题名校验、过滤器校验与匹配
  broker.rs   会话表、包ID分配（回绕跳过在飞）、inflight 重发状态、
              入向去重、保留消息、订阅路由、离线队列
  server.rs   TCP 服务器（每连接一读线程+一写线程）
  bin/mqtt_server.rs  服务器入口
examples/
  mqtt_client.rs  样例客户端（sub/pub）
  hexcheck.rs     打印样例报文的实际编码字节
tests/
  integration.rs  端到端验收测试（真实 TCP）
samples/
  requests.md     字节级请求样例（含 nc 手工验证）
```

## 运行

```bash
cargo build
cargo run --bin mqtt-server -- --addr 127.0.0.1:18830 --max-packet 65536

# 另开终端：订阅
cargo run --example mqtt_client -- sub --id sub1 --topic 'sensors/+'
# 发布（QoS1 + 保留）
cargo run --example mqtt_client -- pub --id pub1 --topic sensors/temp \
    --payload '21.5' --retain
```

## 测试

```bash
cargo test
```

覆盖的验收场景（`tests/integration.rs`，真实 TCP 连接）：

| 测试 | 验收点 |
|---|---|
| `puback_loss_triggers_dup_retransmit_on_reconnect` | PUBACK 丢失：持久会话重连后同包 ID 以 DUP=1 重发，确认后不再重发 |
| `duplicate_publish_is_deduplicated` | 重复 PUBLISH：DUP=1 重发只投递一次，仍回 PUBACK |
| `packet_id_reuse_after_puback` | 包 ID 复用：PUBACK 完成后同 ID 作为新消息正常投递 |
| `session_reconnect_persistent_vs_clean` | 会话重连：clean=false 恢复订阅+离线补投；clean=true 清空 |
| `retained_message_and_filter_matching` | 保留消息按过滤器匹配下发（retain=1），空载荷清除 |
| `basic_connect_subscribe_publish_qos1` | CONNECT/SUBSCRIBE/QoS1 发布全链路 + PING |
| `will_delivered_on_abnormal_disconnect` | 异常断连投递遗嘱 |
| `protocol_errors_close_connection` | 首包非 CONNECT / 超限 / QoS2 / 重复 CONNECT → 关闭连接 |
| `oversized_packet_limit_is_configurable` | 上限可配置，32KB 消息在 64KB 上限下通过 |

单元测试另覆盖：逐字节增量解析、一缓冲多包、剩余长度畸形、标志位非法、
保留类型（0/15）、主题匹配规则、包 ID 回绕跳过在飞、clean/persistent 会话行为。

## 实测记录

环境：Linux 6.8.0-90-generic，rustc 1.98.1，cargo 1.98.1。

```
$ cargo test
   Compiling mqtt-subset v0.1.0
    Finished `test` profile [unoptimized + debuginfo] target(s)

running 14 tests (src 单元测试)            ... 14 passed; 0 failed
running 9 tests  (tests/integration.rs)    ...  9 passed; 0 failed

test result: ok. 23 passed; 0 failed; 0 ignored
```

开发过程中曾失败并已修复的项（如实记录）：

1. `packet_id_reuse_after_puback` 初版失败：入向去重仅凭包 ID，导致
   PUBACK 完成后复用同 ID 的新消息被误判为重复。修复：仅 DUP=1 才命中
   去重窗口（`broker.rs::publish_from_client`）。
2. `retained_message_and_filter_matching` 初版失败：QoS0 保留消息写入后
   立即断言服务器状态存在竞态。修复：测试改用 QoS1 + PUBACK 同步。
3. `oversized_packet_limit_is_configurable` 初版失败：测试客户端解码器
   沿用了服务器的 1KB 小上限。修复：客户端解码器使用默认 64KB。

端到端手工验证（实际运行输出）：

```
$ mqtt-server --addr 127.0.0.1:18830
mqtt-server ... listening on 127.0.0.1:18830

$ mqtt_client sub --id sub1 --topic 'sensors/+'
CONNACK session_present=false return_code=0
SUBACK granted=[1]
PUBLISH topic=sensors/temp qos=1 dup=false retain=false id=Some(1) payload="21.5"

$ mqtt_client pub --id pub1 --topic sensors/temp --payload '21.5' --retain
CONNACK session_present=false return_code=0
PUBACK id=1

# 新订阅者立即收到保留消息（retain=true）：
$ mqtt_client sub --id sub2 --topic 'sensors/#'
PUBLISH topic=sensors/temp qos=1 dup=false retain=true id=Some(1) payload="21.5"

# 原始字节验证：
$ printf '\x10\x10\x00\x04MQTT\x04\x02\x00\x3c\x00\x04sub1' | nc 127.0.0.1 18831 | xxd
00000000: 2002 0000                                 # CONNACK accepted
```

## 已知限制

- 无定时器重传（仅重连时重发）；无 keepalive 超时强制踢线。
- 入向去重窗口随会话存续，clean 会话断开即清。
- 单进程内存状态，无持久化；无 TLS/鉴权（username/password 仅解析）。
- UNSUBSCRIBE 未实现；QoS2 明确拒绝。
