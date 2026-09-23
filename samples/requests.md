# 请求样例（MQTT 3.1.1 子集，原始字节）

以下字节均为十六进制，可直接用 `nc` / Python socket 复现。脚本版见
[`mqtt_raw_demo.py`](./mqtt_raw_demo.py)（无第三方依赖）。

## 1. CONNECT（CleanSession=1, ClientID="c1", KeepAlive=60）

```
10 11
00 04 4d 51 54 54     ; 协议名长度=4, "MQTT"
04                    ; 协议级别 4（MQTT 3.1.1）
02                    ; Connect Flags: CleanSession=1
00 3c                 ; Keep Alive = 60
00 02 63 31           ; ClientID 长度=2, "c1"
```

期望响应（CONNACK，4 字节）：

```
20 02 00 00           ; Session Present=0, Return Code=0（接受）
```

## 2. SUBSCRIBE（id=10, 过滤器 "chat/1" QoS1）

```
82 0a                 ; SUBSCRIBE，固定头低4位固定 0010
00 0a                 ; 包标识符 10
00 06 63 68 61 74 2f 31  ; 过滤器长度=6, "chat/1"
01                    ; 请求 QoS 1
```

期望响应：

```
90 03 00 0a 01        ; SUBACK id=10，授予 QoS1（请求 QoS2 时也授予 1）
```

## 3. PUBLISH QoS1（topic="chat/1", id=500, payload="first"）

```
32 0d                 ; PUBLISH, QoS1（flags=0010）, remaining=13
00 06 63 68 61 74 2f 31
01 f4                 ; 包标识符 500
66 69 72 73 74        ; "first"
```

期望响应：`40 02 01 f4`（PUBACK 500）。

## 4. PUBACK 丢失后的重发（验收核心）

订阅者收到 broker 发出的 QoS1 PUBLISH 后**不回 PUBACK**，等待超过
`--retry-ms`（默认 2000ms）后会收到第二份：

```
3a ..                 ; 首字节 0x32 -> 0x3a：DUP 位置 1（0b1000），QoS 仍为 1
...                   ; 包标识符与载荷与第一份完全相同
```

- 相同包标识符；
- DUP=1；
- 收到重发后补 `40 02 <id高> <id低>` 即停止重发。

## 5. 保留消息

发布（RETAIN 位 = 固定头最低位 1，0x33）：

```
33 0f 00 06 64 65 6d 6f 2f 73 74 61 74 75 73 01 f4 6f 6e 6c 69 6e 65
   ; topic="demo/status", id=500(示例), payload="online"
```

此后**新**订阅 `demo/status` 的客户端会在 SUBACK 后立即收到一份 RETAIN=1
（首字节 0x33）的同名同载荷消息。用空载荷 + RETAIN=1 可删除保留消息。

## 6. 重复 PUBLISH（客户端重传）

同一 QoS1 内容用相同包标识符发两次（第二次 DUP=1，即首字节 0x3a）：
broker 两次都回 PUBACK，但只向订阅者转发一次
（计数器 `dup_suppressed` 增加）。

## 7. 心跳 / 断开

```
c0 00                 ; PINGREQ  ->  d0 00 PINGRESP
e0 00                 ; DISCONNECT（CleanSession=1 会话随即删除；
                       ;              CleanSession=0 会话保留）
```

## 8. 子集拒绝样例

| 输入 | 行为 |
|---|---|
| CONNECT 协议级别字节为 `03` | CONNACK `20 02 00 01`（不可接受的协议版本）后断连 |
| CONNECT 空 ClientID + CleanSession=0 | CONNACK `20 02 00 02`（标识符被拒绝） |
| CONNECT Will Flag=1 | CONNACK `20 02 00 03`（子集不支持遗嘱） |
| 连接后发 QoS2 PUBLISH（0x34） | 直接关闭 TCP（不做降级） |
| 剩余长度超过 `--max-packet` | 直接关闭 TCP |
