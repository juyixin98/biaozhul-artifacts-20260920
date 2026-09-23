#!/usr/bin/env python3
"""MQTT 3.1.1 子集 —— 原始 TCP 请求样例（无第三方依赖，仅用标准库）。

不依赖任何 MQTT 客户端库，手工拼装/解析 MQTT 字节，便于逐字节核对报文结构。
运行前先启动 broker：
    cargo run --bin mqtt-subset-broker -- --addr 127.0.0.1:18830 --retry-ms 1000

用法（同一台机器开两个终端）：
    python3 samples/mqtt_raw_demo.py subscriber 127.0.0.1:18830
    python3 samples/mqtt_raw_demo.py publisher  127.0.0.1:18830

publisher 场景会依次演示：
  1) 普通 QoS1 PUBLISH（正常 PUBACK）；
  2) PUBACK 丢失模拟（订阅端不确认，观察 broker DUP=1 重发）；
  3) 保留消息（RETAIN=1）；
  4) 重复 PUBLISH（同包ID同内容，观察只转发一次但两次 PUBACK）。
"""

import socket
import struct
import sys
import time


def encode_remaining_length(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        if n > 0:
            b |= 0x80
        out.append(b)
        if n == 0:
            return bytes(out)


def mlstr(b: bytes) -> bytes:
    return struct.pack("!H", len(b)) + b


def frame(first: int, body: bytes) -> bytes:
    return bytes([first]) + encode_remaining_length(len(body)) + body


def connect(client_id: str, clean: bool, keep_alive: int = 60) -> bytes:
    body = b""
    body += mlstr(b"MQTT")
    body += bytes([4])  # 协议级别 4 = MQTT 3.1.1
    body += bytes([0x02 if clean else 0x00])
    body += struct.pack("!H", keep_alive)
    body += mlstr(client_id.encode())
    return frame(0x10, body)


def publish(topic: str, payload: bytes, qos: int, packet_id=None,
            dup=False, retain=False) -> bytes:
    first = 0x30
    if dup:
        first |= 0b1000
    first |= (qos & 0b11) << 1
    if retain:
        first |= 0b0001
    body = mlstr(topic.encode())
    if qos > 0:
        assert packet_id is not None and packet_id != 0
        body += struct.pack("!H", packet_id)
    body += payload
    return frame(first, body)


def puback(packet_id: int) -> bytes:
    return frame(0x40, struct.pack("!H", packet_id))


def subscribe(packet_id: int, filters) -> bytes:
    body = struct.pack("!H", packet_id)
    for f, qos in filters:
        body += mlstr(f.encode()) + bytes([qos])
    return frame(0x82, body)


def disconnect() -> bytes:
    return frame(0xE0, b"")


def read_frame(sock) -> tuple[int, bytes]:
    def recv1():
        b = sock.recv(1)
        if not b:
            raise ConnectionError("connection closed by broker")
        return b[0]

    first = recv1()
    multiplier = 1
    remaining = 0
    for _ in range(4):
        b = recv1()
        remaining += (b & 0x7F) * multiplier
        if b & 0x80 == 0:
            body = b""
            while len(body) < remaining:
                chunk = sock.recv(remaining - len(body))
                if not chunk:
                    raise ConnectionError("connection closed mid-frame")
                body += chunk
            return first, body
        multiplier *= 128
    raise ValueError("remaining length too long")


def parse_publish(first: int, body: bytes):
    qos = (first >> 1) & 0b11
    dup = bool(first & 0b1000)
    retain = bool(first & 0b0001)
    tlen = struct.unpack("!H", body[:2])[0]
    pos = 2
    topic = body[pos:pos + tlen].decode()
    pos += tlen
    pid = None
    if qos > 0:
        pid = struct.unpack("!H", body[pos:pos + 2])[0]
        pos += 2
    return topic, pid, body[pos:], qos, dup, retain


def wait_connack(sock):
    first, body = read_frame(sock)
    assert first == 0x20, f"expected CONNACK got {first:#x}"
    sp, rc = body[0], body[1]
    print(f"<- CONNACK session_present={sp} return_code={rc}")
    return rc


def connect_addr(addr: str):
    host, port = addr.rsplit(":", 1)
    return (host, int(port))


def run_subscriber(addr: str):
    s = socket.create_connection(connect_addr(addr))
    s.settimeout(10)
    s.sendall(connect("sample-sub", clean=False))
    rc = wait_connack(s)
    assert rc == 0

    # 两个订阅：精确 + 通配符。
    s.sendall(subscribe(200, [("demo/temp", 1), ("demo/+", 1)]))
    first, body = read_frame(s)
    assert first == 0x90
    print(f"<- SUBACK id={struct.unpack('!H', body[:2])[0]} codes={list(body[2:])}")

    print("[subscriber] listening (Ctrl-C to stop)...")
    while True:
        try:
            first, body = read_frame(s)
        except socket.timeout:
            continue
        ptype = first >> 4
        if ptype == 3:
            topic, pid, payload, qos, dup, retain = parse_publish(first, body)
            print(f"<- PUBLISH topic={topic} qos={qos} id={pid} dup={dup} "
                  f"retain={retain} payload={payload!r}")
            if pid is not None:
                s.sendall(puback(pid))
                print(f"-> PUBACK {pid}")
        elif ptype == 9:
            print("<- SUBACK")
        elif ptype == 13:
            print("<- PINGRESP")
        else:
            print(f"<- packet type {ptype}")


def run_publisher(addr: str):
    s = socket.create_connection(connect_addr(addr))
    s.settimeout(5)
    s.sendall(connect("sample-pub", clean=True))
    assert wait_connack(s) == 0

    print("\n[1] 普通 QoS1 PUBLISH demo/temp (id=1001)")
    s.sendall(publish("demo/temp", b"hello-at-least-once", qos=1, packet_id=1001))
    first, body = read_frame(s)
    print(f"<- PUBACK id={struct.unpack('!H', body)[0]}")

    print("\n[2] RETAIN=1 PUBLISH demo/status：无订阅者时也会被存储")
    s.sendall(publish("demo/status", b"online=true", qos=1, packet_id=1002, retain=True))
    first, body = read_frame(s)
    print(f"<- PUBACK id={struct.unpack('!H', body)[0]}")

    print("\n[3] 重复 PUBLISH：相同 id=1003 相同内容发两次")
    for i in range(2):
        s.sendall(publish("demo/temp", b"dup-body", qos=1, packet_id=1003, dup=(i == 1)))
        first, body = read_frame(s)
        print(f"<- PUBACK id={struct.unpack('!H', body)[0]} (第 {i+1} 次，broker 每次都回)")

    print("\n[4] 通配符：发布 demo/humidity，订阅 demo/+ 者也能收到")
    s.sendall(publish("demo/humidity", b"55%", qos=1, packet_id=1004))
    first, body = read_frame(s)
    print(f"<- PUBACK id={struct.unpack('!H', body)[0]}")

    print("\n[5] PUBACK 丢失模拟：由该脚本自身作为临时订阅者，收到后不确认，")
    print("    broker 会以相同 id、DUP=1 重发（等待 ~2 个重发周期后退出）。")
    sub = socket.create_connection(connect_addr(addr))
    sub.settimeout(5)
    sub.sendall(connect("sample-watch", clean=True))
    assert wait_connack(sub) == 0
    sub.sendall(subscribe(300, [("demo/lost", 1)]))
    read_frame(sub)  # SUBACK

    s.sendall(publish("demo/lost", b"resend-me", qos=1, packet_id=1005))
    first, body = read_frame(s)
    print(f"<- (publisher) PUBACK id={struct.unpack('!H', body)[0]}")

    f1, b1 = read_frame(sub)
    t, pid, p, q, dup, ret = parse_publish(f1, b1)
    print(f"<- (watch) 首次: id={pid} dup={dup} payload={p!r}  [故意不回 PUBACK]")
    f2, b2 = read_frame(sub)
    t, pid2, p2, q, dup2, ret = parse_publish(f2, b2)
    print(f"<- (watch) 重发: id={pid2} dup={dup2} payload={p2!r}")
    assert pid == pid2 and dup2 and p == p2, "PUBACK 丢失后必须 DUP=1 同 id 重发"
    sub.sendall(puback(pid2))
    print(f"-> (watch) PUBACK {pid2}，重发循环结束")
    time.sleep(0.5)

    s.sendall(disconnect())
    sub.sendall(disconnect())
    print("\n全部样例完成。")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] not in ("publisher", "subscriber"):
        print(__doc__)
        sys.exit(2)
    run_subscriber(sys.argv[2]) if sys.argv[1] == "subscriber" else run_publisher(sys.argv[2])
