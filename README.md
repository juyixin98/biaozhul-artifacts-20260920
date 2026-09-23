# MQTT 3.1.1 会话子集（纯后端）

用 Rust 从零实现的 **MQTT 3.1.1 会话子集**：包含一个**增量字节解析库**与一个
**本地 TCP 测试服务**。无任何第三方 crate 依赖（仅 `std`），未使用任何现成的
MQTT 协议解析器——编解码全部在 [`src/codec.rs`](src/codec.rs) 内手工实现。

> **交付语义：仅「至少一次」（at-least-once）。** QoS1 消息在收到 PUBACK 前
> 会以 `DUP=1` 持续重发，因此消费者**可能收到重复消息**。本项目**不提供、也不
> 宣称**业务层「恰好一次」（exactly-once）。QoS2（PUBREC/PUBREL/PUBCOMP）不
> 属于本子集。

---

## 1. 目录结构

```
src/
  error.rs            显式错误类型 CodecError + CONNACK 返回码 ConAckReason
  packet.rs           报文数据模型（CONNECT/PUBLISH/PUBACK/SUBSCRIBE/…）
  codec.rs            从零实现的增量解析器 Decoder + 编码器（核心，无依赖）
  topic.rs            主题名校验、通配符过滤器（+ / #）匹配
  session.rs          会话状态：订阅、包标识符分配/复用、入站去重、离线队列、在途表
  broker.rs           本地 TCP 测试服务：路由、保留消息、重发定时器、保活
  bin/broker.rs       可执行文件入口（CLI 参数、SIGTERM 统计）
examples/
  client_demo.rs      极简 Rust 客户端（pub/sub），演示手工帧
samples/
  mqtt_raw_demo.py    无第三方依赖的 Python 原始字节演示（pub/sub，含 PUBACK 丢失）
  requests.md         十六进制请求样例与期望响应
tests/
  common/mod.rs                 测试工具（帧构造/读取、随机端口 broker）
  parser_tests.rs               解析器：半包/粘包、错误类型、长度上限、边界字节
  acceptance_puback_loss.rs     验收：PUBACK 丢失 → DUP=1 重发
  acceptance_duplicate_publish.rs 验收：重复 PUBLISH（只转发一次，两次 PUBACK）
  acceptance_packet_id_reuse.rs 验收：入站/出站包标识符复用
  acceptance_session_reconnect.rs 验收：持久会话重连/离线队列/CleanSession
  acceptance_retained_and_match.rs 验收：保留消息 + 通配符匹配 + 删除/降级
  acceptance_subset_boundaries.rs 验收：拒绝码、QoS2、长度上限、保活
```

## 2. 子集边界（明确支持 / 不支持）

### 支持的报文

| 方向 | 报文 |
|---|---|
| 入站（客户端→服务端） | CONNECT, PUBLISH(**QoS0/QoS1**), PUBACK, SUBSCRIBE, PINGREQ, DISCONNECT |
| 出站（服务端→客户端） | CONNACK, PUBLISH(QoS0/QoS1), PUBACK, SUBACK, PINGRESP |

支持的会话特性：CleanSession 0/1、Session Present 标志、Keep Alive（1.5 倍
宽限断开）、QoS0/QoS1 转发（有效 QoS = `min(发布QoS, 订阅QoS)`）、保留消息
（RETAIN，含空载荷删除）、主题通配符 `+`/`#`、QoS1 在途重发、持久会话离线
排队（FIFO）、包标识符分配与复用、入站重复 PUBLISH 抑制。

### 明确**不**支持（遇到时的行为也已定义，不会静默出错）

| 能力 | 行为 |
|---|---|
| **QoS2**（PUBLISH QoS=2 / PUBREC / PUBREL / PUBCOMP） | 连接后收到 QoS2 PUBLISH：按协议错误**关闭 TCP**；收到未实现类型：解析错误断开 |
| **遗嘱** Last Will & Testament | CONNECT 带 Will Flag：回 **CONNACK 0x03**（服务不可用）后断连 |
| UNSUBSCRIBE / SUBACK 以外的控制报文（AUTH 等） | `InvalidPacketType` 解析错误 → 断连 |
| MQTT 3.1（协议级别 3）/ 其它协议名 | CONNACK **0x01**（协议名不为 `MQTT` 时直接断连） |
| 认证（UserName/Password 字段可解析，但不校验） | 不做鉴权；保留字段以便返回 0x04/0x05 |

> 协议错误处理遵循 MQTT 3.1.1 §4.13：除 CONNECT 阶段可用 CONNACK 返回码表达
> 的错误外，其余协议违例直接关闭传输层连接。

## 3. 长度上限与错误类型

- **剩余长度上限**：单报文剩余长度默认 `256 KiB`（`--max-packet` 可调；协议
  理论上限 268,435,455）。超限返回 `CodecError::PayloadTooLarge{limit,actual}`，
  broker 随即断连，不会无界分配内存。
- **剩余长度编码**：最多 4 字节；第 5 个续位字节 → `MalformedRemainingLength`。
- 解析器错误为显式枚举 `CodecError`（见
  [src/error.rs](src/error.rs)）：`NeedMoreData`（增量解析的正常「未就绪」，
  非错误）、`PayloadTooLarge`、`LengthExceedsBuffer`、`InvalidPacketType`、
  `InvalidProtocolName`、`UnsupportedProtocolLevel`、`InvalidReservedFlag`、
  `MalformedConnect`、`MalformedPacket`、`InvalidUtf8`、`InvalidQoS`、
  `InvalidTopicName`、`EmptySubscriptionList`、`InvalidTopicFilter`、
  `PacketIdOnQos0`、`MissingPacketId`。

## 4. 增量解析器设计要点（`src/codec.rs`）

TCP 是字节流，一个 MQTT 报文可能横跨多次 `read()`（半包），也可能多个报文粘在
一次 `read()` 里（粘包）。`Decoder` 内部维护一个增长缓冲：

```text
socket.read() -> Decoder.feed(bytes)
loop { match Decoder.try_parse() {
    Ok(Some(packet)) => 处理（已消费字节自动丢弃，粘包的后续报文留在缓冲头部）,
    Ok(None)         => 字节不足，break 继续等 socket（已有字节原样保留）,
    Err(e)           => 协议错误，按 §4.13 关闭连接,
}}
```

- 分两阶段：先只解析固定头（类型/标志/变长剩余长度），能确定整帧长度后才解析
  报文体——半包时**绝不**触碰报文体字段；
- 长度前缀字段越界（声称 N 字节但剩余不足）→ `LengthExceedsBuffer`；
- 解析输出为 owned 报文（`String`/`Vec<u8>`），可跨缓冲压缩安全持有。

## 5. 包标识符、重发状态与「至少一次」

- **出站（broker→订阅者）**：每条 QoS1 消息分配一个当前空闲的 1..=65535 包标识
  符（最小可用策略，PUBACK 后立即释放复用，0 永不分配），记入 `inflight` 表；
  重发定时器（默认 2s，200ms 扫描粒度）对超时在途消息以**相同包标识符 +
  DUP=1** 重发；收到 PUBACK 才移除。PUBACK 丢失 → 客户端必然收到重复。
- **入站（发布者→broker）**：对 QoS1 PUBLISH 记录
  `包标识符 → 内容指纹(主题|载荷|QoS|RETAIN)`。
  - 同 ID + 同指纹 = 重传：**只补 PUBACK，不再转发**（抑制重复）；
  - 同 ID + 不同指纹 = 旧事务结束后的 ID 复用：视为**新消息**；
  - 重复 PUBLISH 每次都会收到 PUBACK（幂等应答，客户端靠它停止重传）。
  指纹去重是**尽力**优化，不改变「至少一次」的本质。
- **持久会话重连**（CleanSession=0）：CONNACK Session Present=1；先以 DUP=1
  重发断线前未确认的在途消息（保持原包标识符），再按 FIFO 投递离线期间排队的
  QoS1 消息（新消息 DUP=0、新分配包标识符）。订阅随会话保留，无需重新 SUBSCRIBE。

## 6. 构建与运行

```bash
cargo build --release

# 启动本地测试服务（Ctrl-C / SIGTERM 会打印计数后退出）
./target/release/mqtt-subset-broker --addr 127.0.0.1:1883 --retry-ms 2000

# 可选参数
#   --addr <IP:PORT>     默认 127.0.0.1:1883
#   --max-packet <字节>  单包剩余长度上限（默认 262144）
#   --retry-ms <毫秒>    QoS1 在途重发间隔（默认 2000）
#   --stats-interval <秒> 周期打印计数（默认 0，仅退出时打印）
#   --quiet              关闭逐事件日志
```

### 快速演示

```bash
# 终端 1：Python 原始字节订阅者（无第三方依赖）
python3 samples/mqtt_raw_demo.py subscriber 127.0.0.1:1883

# 终端 2：Python 发布者（含 PUBACK 丢失/保留消息/重复 PUBLISH/通配符）
python3 samples/mqtt_raw_demo.py publisher 127.0.0.1:1883

# 或使用 Rust 示例客户端
cargo run --example client_demo -- sub 127.0.0.1:1883 sub1 "demo/+" 1
cargo run --example client_demo -- pub 127.0.0.1:1883 pub1 "demo/temp" 1 "hi"
```

十六进制请求/响应样例见 [samples/requests.md](samples/requests.md)。

## 7. 测试

```bash
cargo test            # 全部单元测试 + 集成/验收测试（每个用例启动随机端口 broker）
cargo clippy --all-targets   # 无警告
cargo fmt --check
```

### 验收场景与用例对应

| 验收点 | 测试文件 / 用例 |
|---|---|
| **PUBACK 丢失** → 同 id、DUP=1 重发；补 ACK 后停止 | `acceptance_puback_loss.rs` |
| **重复 PUBLISH** → 转发一次、PUBACK 两次、计数器=1 | `acceptance_duplicate_publish.rs` |
| **包ID复用**（入站同 ID 新内容=新消息；出站 ACK 后立即复用） | `acceptance_packet_id_reuse.rs`（2 用例） |
| **会话重连**（离线 FIFO、在途 DUP 重发、Session Present、Clean 丢弃、clean 覆盖持久会话） | `acceptance_session_reconnect.rs`（4 用例） |
| **保留消息**（新订阅 RETAIN=1、在线投递 RETAIN=0、覆盖、空载荷删除、QoS 降级） | `acceptance_retained_and_match.rs`（4 用例） |
| **订阅匹配**（`+` 单级、`#` 多级、`$` 主题、实时/保留） | `acceptance_retained_and_match.rs` + `topic.rs` 单测 |
| 拒绝码 0x01/0x02/0x03、QoS2 断开、长度上限断连、保活、首帧非 CONNECT、未知 PUBACK | `acceptance_subset_boundaries.rs`（10 用例） |
| 半包逐字节/粘包/错误类型/编码器字节 | `parser_tests.rs`（11 用例）+ 编解码/会话/主题单测（13 个） |

## 8. 已知限制（如实声明）

- 仅保证**至少一次**；不实现 QoS2，不提供去重后的端到端恰好一次保证。
- 会话与保留消息保存在**进程内存**，无持久化落盘；进程重启即丢失（定位是
  「本地 TCP 测试服务」）。
- 每会话离线队列上限 1000 条（超出丢弃最旧并计数 `dropped`，符合 §3.1.2.4
  允许服务端删除排队消息的规定）；在途包标识符池 65535 满时形成背压（计入
  `dropped`）。
- 无 TLS、无鉴权、无 ACL、无 WebSocket；不做共享订阅/主题别名（那是 MQTT5）。
- 并发模型为「每连接一线程 + 全局会话锁」，面向功能验收与本地测试，未做高吞吐
  优化。

## 9. 规范依据

MQTT Version 3.1.1 (OASIS Standard, 2014)：§2.2 固定头与剩余长度、§2.3.1
包标识符、§3.1 CONNECT、§3.1.2.4 Clean Session、§3.1.3 ClientID、§3.2
CONNACK 返回码、§3.3 PUBLISH（DUP/QoS/RETAIN）、§3.3.1.3 保留消息、§3.4
PUBACK、§3.8/§3.9 SUBSCRIBE/SUBACK、§3.12/§3.13 PING、§3.14 DISCONNECT、
§4.7 主题匹配、§4.13 协议错误处理。
