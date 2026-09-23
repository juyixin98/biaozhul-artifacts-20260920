#!/usr/bin/env python3
"""
acceptance.py — 独立于 Go 测试之外的验收脚本（Python 标准库，零依赖）。

它自己实现一个 RESP2 编码器（字符串拼接，不与 Go 生产代码共享任何实现），
然后通过原始 TCP 套接字访问 HTTP 接口，覆盖题目要求的四类验收点：

  1. 协议按“单字节”发送（整个 HTTP 请求每个 write 一个字节）；
  2. 负长度 / 非法帧（$-2、*-3、声明长度不符）；
  3. 半包断连（Content-Length 声明大于实际发送字节数后立即 FIN）；
  4. 事务中语法错误（未知命令、参数个数错误导致 EXECABORT）；
  外加：空值与空字符串区分、与独立编码器的往返对照。

用法：
    python3 examples/acceptance.py [host:port]
脚本默认先启动 `go run .`，也可以连接已经运行的服务器。
退出码 0 表示全部通过。
"""

import http.client
import socket
import subprocess
import sys
import time
import os
from urllib.parse import urlparse

PASS, FAIL = 0, 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


# --------------------------------------------------------------------------
# 独立 RESP2 编码器 —— 刻意只用字符串拼接
# --------------------------------------------------------------------------
def enc_bulk(b: bytes) -> bytes:
    return b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n"


def enc_cmd(*parts) -> bytes:
    out = b"*" + str(len(parts)).encode() + b"\r\n"
    for p in parts:
        if isinstance(p, str):
            p = p.encode()
        out += enc_bulk(p)
    return out


# --------------------------------------------------------------------------
# 极简 RESP2 解码器（同样是独立实现）
# --------------------------------------------------------------------------
class Decoder:
    def __init__(self, data: bytes):
        self.b = data
        self.i = 0

    def line(self):
        j = self.b.index(b"\r\n", self.i)
        s = self.b[self.i:j]
        self.i = j + 2
        return s

    EOF = object()

    def next(self):
        if self.i >= len(self.b):
            return Decoder.EOF
        t = self.b[self.i]
        self.i += 1
        line = self.line()
        if t == ord("+"):
            return ("simple", line.decode())
        if t == ord("-"):
            return ("error", line.decode())
        if t == ord(":"):
            return ("integer", int(line))
        if t == ord("$"):
            n = int(line)
            if n == -1:
                return ("null-bulk", None)
            v = self.b[self.i:self.i + n]
            self.i += n + 2
            return ("bulk", v)
        if t == ord("*"):
            n = int(line)
            if n == -1:
                return ("null-array", None)
            return ("array", [self.next() for _ in range(n)])
        raise ValueError(f"unknown type byte {chr(t)}")


def decode_all(data):
    d = Decoder(data)
    out = []
    while True:
        v = d.next()
        if v is Decoder.EOF:
            return out
        out.append(v)


# --------------------------------------------------------------------------
# HTTP over raw socket: one byte per write
# --------------------------------------------------------------------------
def raw_post(host, port, path, body: bytes, declare_len=None, one_byte=False,
             extra_headers=""):
    sock = socket.create_connection((host, port), timeout=10)
    length = declare_len if declare_len is not None else len(body)
    extra = extra_headers
    if extra and not extra.endswith("\r\n"):
        extra += "\r\n"
    req = (
        f"POST {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {length}\r\nConnection: close\r\n"
        f"{extra}\r\n"
    ).encode() + body
    if one_byte:
        for ch in req:
            sock.sendall(bytes([ch]))
    else:
        sock.sendall(req)
    chunks = []
    try:
        while True:
            c = sock.recv(65536)
            if not c:
                break
            chunks.append(c)
    except (socket.timeout, ConnectionError):
        pass
    sock.close()
    return b"".join(chunks)


def parse_http(raw: bytes):
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    status = int(lines[0].split()[1])
    return status, body


def http_post_json(url, payload):
    conn = http.client.HTTPConnection(url.netloc, timeout=10)
    conn.request("POST", url.path, body=payload,
                 headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    data = r.read()
    conn.close()
    return r.status, data


def main():
    started = None
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if ":" not in target:
            target += ":7379"
        host, port_s = target.rsplit(":", 1)
        port = int(port_s)
    else:
        port = 17379
        host = "127.0.0.1"
        env = dict(os.environ)
        started = subprocess.Popen(
            ["go", "run", ".", "-addr", f"127.0.0.1:{port}"],
            cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
        )
        for _ in range(50):
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.2)

    try:
        print(f"== target {host}:{port}")

        # 0) 健康检查
        conn = http.client.HTTPConnection(host, port, timeout=5)
        conn.request("GET", "/healthz")
        r = conn.getresponse()
        check("GET /healthz -> 200", r.status == 200 and b"ok" in r.read())
        conn.close()

        # 1) 流水线 + 单字节发送
        body = b"".join([
            enc_cmd("FLUSHDB"),
            enc_cmd("PING"),
            enc_cmd("PING", "hello"),
            enc_cmd("SET", "empty", ""),
            enc_cmd("GET", "empty"),
            enc_cmd("GET", "missing"),
            enc_cmd("INCR", "n"),
            enc_cmd("INCR", "n"),
        ])
        status, reply = parse_http(raw_post(host, port, "/resp", body,
                                            one_byte=True))
        check("pipeline sent ONE BYTE per write -> 200", status == 200,
              f"status={status}")
        vals = decode_all(reply)
        check("pipeline: 8 ordered replies", len(vals) == 8, f"{len(vals)}")
        check("PING -> +PONG", vals[1] == ("simple", "PONG"), str(vals[1]))
        check("PING hello -> bulk hello", vals[2] == ("bulk", b"hello"))
        check("empty string is bulk(\"\") not null",
              vals[4] == ("bulk", b""), str(vals[4]))
        check("missing key is null-bulk", vals[5] == ("null-bulk", None),
              str(vals[5]))
        check("INCR sequence 1 then 2",
              vals[6] == ("integer", 1) and vals[7] == ("integer", 2))

        # 二进制安全往返：\0 \r \n 0xff
        binary = bytes([0, 1, 13, 10, 255])
        body = enc_cmd("SET", "bin", binary) + enc_cmd("GET", "bin")
        _, reply = parse_http(raw_post(host, port, "/resp", body,
                                       one_byte=True))
        vals = decode_all(reply)
        check("binary-safe round trip incl. CR/LF/0x00/0xff",
              vals[1] == ("bulk", binary), str(vals[1]))

        # 2) 负长度与非法帧
        for label, bad in [
            ("bulk length -2", b"$-2\r\n"),
            ("array length -3", b"*-3\r\n"),
            ("declared length mismatch", b"$5\r\nabc\r\n"),
            ("non-numeric length", b"$xx\r\n"),
        ]:
            status, reply = parse_http(raw_post(host, port, "/resp", bad))
            vals = decode_all(reply)
            check(f"{label}: 400 + error reply",
                  status == 400 and vals and vals[0][0] == "error"
                  and "Protocol error" in vals[0][1],
                  f"status={status} vals={vals}")

        # 3) 半包断连：声称 Content-Length 很大，只发半个 bulk 就 FIN
        half = enc_cmd("SET", "k1", "v1") + b"*3\r\n$3\r\nSET\r\n$2\r\nk2\r\n$100\r\nabc"
        raw_post(host, port, "/resp", half, declare_len=len(half) + 200)
        # 服务器必须仍然健康
        time.sleep(0.1)
        status, reply = parse_http(raw_post(host, port, "/resp",
                                            enc_cmd("PING")))
        vals = decode_all(reply)
        check("half-packet disconnect: server survives, PING works",
              status == 200 and vals[0] == ("simple", "PONG"))

        # 半包但 Content-Length 精确匹配：400 + 已完成命令的回复 + 终止错误
        status, reply = parse_http(raw_post(host, port, "/resp", half))
        vals = decode_all(reply)
        check("in-body half packet: 400, prior reply kept, terminal error",
              status == 400 and len(vals) == 2
              and vals[0] == ("simple", "OK")
              and vals[1][0] == "error",
              f"status={status} vals={vals}")

        # 4) 事务：语法错误 -> EXECABORT，且任何排队命令都不执行
        body = b"".join([
            enc_cmd("MULTI"),
            enc_cmd("SET", "a", "1"),
            enc_cmd("BOGUS", "x"),
            enc_cmd("SET", "b", "2"),
            enc_cmd("EXEC"),
        ])
        _, reply = parse_http(raw_post(host, port, "/resp", body,
                                       one_byte=True))
        vals = decode_all(reply)
        check("tx unknown command: queue-time error",
              "unknown command" in vals[2][1], str(vals[2]))
        check("tx unknown command: EXECABORT",
              vals[4][0] == "error" and vals[4][1].startswith("EXECABORT"),
              str(vals[4]))
        _, reply = parse_http(raw_post(host, port, "/resp",
                                       enc_cmd("EXISTS", "a", "b")))
        check("aborted tx executed nothing (EXISTS == 0)",
              decode_all(reply)[0] == ("integer", 0))

        # 参数个数错误同样中止
        body = b"".join([enc_cmd("MULTI"), enc_cmd("GET"), enc_cmd("EXEC")])
        _, reply = parse_http(raw_post(host, port, "/resp", body))
        vals = decode_all(reply)
        check("tx arity error -> EXECABORT",
              "wrong number" in vals[1][1]
              and vals[2][1].startswith("EXECABORT"), str(vals))

        # 成功事务：EXEC 返回数组，运行期错误只影响该条
        body = b"".join([
            enc_cmd("SET", "s", "notint"),
            enc_cmd("MULTI"),
            enc_cmd("INCR", "s"),
            enc_cmd("SET", "after", "1"),
            enc_cmd("EXEC"),
        ])
        _, reply = parse_http(raw_post(host, port, "/resp", body,
                                       one_byte=True))
        vals = decode_all(reply)
        arr = vals[4]
        check("EXEC result is an array",
              arr[0] == "array" and len(arr[1]) == 2, str(arr))
        check("runtime error stays inside array",
              arr[1][0][0] == "error" and arr[1][1][0] == "simple"
              and arr[1][1][1] == "OK", str(arr))

        # 5) 独立编码器对照：两种写法（本脚本 vs Go Encoder 由 Go 测试保证）
        #    这里检查响应帧可以用独立解码器完整还原，且顺序不漂移。
        cmds = [enc_cmd("MSET", "x", "1", "y", "2"),
                enc_cmd("MGET", "x", "y", "z"),
                enc_cmd("INCRBY", "x", "9"),
                enc_cmd("KEYS", "*")]
        _, reply = parse_http(raw_post(host, port, "/resp",
                                       b"".join(cmds), one_byte=True))
        vals = decode_all(reply)
        check("independent encoder round trip order",
              vals[0] == ("simple", "OK")
              and vals[1] == ("array", [
                  ("bulk", b"1"), ("bulk", b"2"), ("null-bulk", None)])
              and vals[2] == ("integer", 10), str(vals[:3]))

        # 6) 会话与跨请求 MULTI
        hdr = "X-Session: acc-1"
        parse_http(raw_post(host, port, "/resp", enc_cmd("MULTI"),
                            extra_headers=hdr))
        parse_http(raw_post(host, port, "/resp",
                            enc_cmd("SET", "k", "v"), extra_headers=hdr))
        _, reply = parse_http(raw_post(host, port, "/resp",
                                       enc_cmd("EXEC"), extra_headers=hdr))
        vals = decode_all(reply)
        check("MULTI across HTTP requests via X-Session",
              vals[0][0] == "array" and vals[0][1][0] == ("simple", "OK"),
              str(vals))

        # 7) JSON 接口冒烟
        st, data = http_post_json(urlparse(f"http://{host}:{port}/exec"),
                                  b'{"command":["SET","j","1"]}')
        check("POST /exec JSON set", st == 200 and b'"OK"' in data,
              f"{st} {data}")
        st, data = http_post_json(urlparse(f"http://{host}:{port}/exec"),
                                  b'{"command":["INCR","j"]}')
        check("POST /exec JSON incr", st == 200 and b'"integer"' in data
              and b'"integer":2' in data, f"{st} {data}")

        print(f"\n== {PASS} passed, {FAIL} failed")
        return 1 if FAIL else 0
    finally:
        if started is not None:
            started.terminate()
            try:
                started.wait(timeout=5)
            except subprocess.TimeoutExpired:
                started.kill()


if __name__ == "__main__":
    sys.exit(main())
