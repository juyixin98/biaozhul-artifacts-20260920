# 请求样例（字节级）

以下样例均可直接发往 `mqtt-server`（默认 `127.0.0.1:18830`）。
十六进制由本库 `codec::encode` 实际生成（生成方式见 `examples/hexcheck.rs`），
`mqtt_client` 示例程序发出的就是这些字节。

## 1. CONNECT（client_id=`sub1`，clean_session=true，keepalive=60s）

```
10 10                                     ; 类型1 CONNECT, 剩余长度 16
  00 04 4D 51 54 54                       ; 协议名 "MQTT"
  04                                      ; 协议级别 4 (MQTT 3.1.1)
  02                                      ; 连接标志: clean_session=1
  00 3C                                   ; keepalive = 60
  00 04 73 75 62 31                       ; 客户端ID "sub1"
```

服务器应答 CONNACK：`20 02 00 00`（session_present=0, accepted）。

## 2. SUBSCRIBE（包ID=1，订阅 `sensors/+`，请求 QoS1）

```
82 0E                                     ; 类型8 SUBSCRIBE(标志0010), 剩余长度 14
  00 01                                   ; 包ID = 1
  00 09 73 65 6E 73 6F 72 73 2F 2B        ; 过滤器 "sensors/+"
  01                                      ; 请求 QoS1
```

服务器应答 SUBACK：`90 03 00 01 01`（包ID=1，授予 QoS1）。

## 3. PUBLISH QoS1（包ID=1，主题 `sensors/temp`，载荷 `21.5`，retain）

```
33 14                                     ; 类型3 PUBLISH(qos1+retain), 剩余长度 20
  00 0C 73 65 6E 73 6F 72 73 2F 74 65 6D 70 ; 主题 "sensors/temp"
  00 01                                   ; 包ID = 1
  32 31 2E 35                             ; 载荷 "21.5"
```

服务器应答 PUBACK：`40 02 00 01`。

## 4. 服务器投递给订阅方的 PUBLISH（QoS1，服务器分配包ID，retain=0）

```
32 14
  00 0C 73 65 6E 73 6F 72 73 2F 74 65 6D 70
  00 01                                   ; 服务器侧包ID
  32 31 2E 35
```

订阅方应答 PUBACK：`40 02 00 01`。

## 5. PINGREQ / PINGRESP / DISCONNECT

```
C0 00                                     ; PINGREQ
D0 00                                     ; PINGRESP（服务器应答）
E0 00                                     ; DISCONNECT
```

## 手工验证（nc / xxd）

```bash
printf '\x10\x10\x00\x04MQTT\x04\x02\x00\x3c\x00\x04sub1' | nc 127.0.0.1 18830 | xxd
# 期望输出: 20 02 00 00 (CONNACK accepted)
```
