#!/usr/bin/env python3
"""brpc-reuse frame tool — pure Python stdlib (struct/socket/zlib).

Serves as independent cross-implementation validation of the Rust parser and
as a generator of request samples / error injections.

Usage:
  frame_tool.py hexdump OP [--id N] [--delay MS] [--text S] [--num A B]
      Print the annotated hex of one REQUEST frame to stdout.
  frame_tool.py send ADDR OP [--id N] ... [--raw]
      Send a frame to a running demo-server and print the decoded reply.
  frame_tool.py dump FILE
      Decode every frame contained in FILE (which may hold several frames
      stuck together) and print them.
  frame_tool.py soak ADDR --n N
      Send N ECHO requests over ONE connection with arbitrary byte-at-a-time
      splitting on our send side is not observable by TCP; instead all N
      frames are written in one syscall (sticky) and replies are read byte by
      byte (half packets), proving interop both ways.
  frame_tool.py inject ADDR KIND
      KIND in: bad-crc | bad-version | oversized | unknown-cmd | bad-flags |
               garbage | half-frame | dup-id | cancel-unknown
      Verify server handling of malformed frames; prints what the server did.

Service ops: echo=1 upper=2 slow=3 add=4 fail=5.
"""

import argparse
import socket
import struct
import sys
import time
import zlib

MAGIC = b"\x42\x51"
VERSION = 1
HEADER_LEN = 13
TRAILER_LEN = 4

CMD = {"request": 1, "response": 2, "cancel": 3, "error": 4, "ping": 5, "pong": 6}
CMD_NAME = {v: k.upper() for k, v in CMD.items()}

OP_ECHO, OP_UPPER, OP_SLOW, OP_ADD, OP_FAIL = 1, 2, 3, 4, 5

ERROR_NAMES = {
    1: "PayloadTooLarge", 2: "CrcMismatch", 3: "UnsupportedVersion",
    4: "UnknownFlag", 5: "UnknownCommand", 6: "UnexpectedEof",
    7: "DuplicateRequest", 8: "ServerBusy", 9: "NoSuchRequest",
    10: "InvalidDirection", 11: "Shutdown", 12: "Cancelled",
    13: "Timeout", 14: "ConnectionClosed", 15: "TooManyInflight",
    16: "Poisoned", 64: "AppError",
}


def crc32(data: bytes) -> int:
    return zlib.crc32(data) & 0xFFFFFFFF


def build_frame(command: int, request_id: int, payload: bytes,
                flags: int = 0, version: int = VERSION, crc: int | None = None) -> bytes:
    """Encode one frame. `crc` may be overridden to inject checksum errors."""
    head = MAGIC + bytes([version, flags, command])
    head += struct.pack(">II", request_id, len(payload))
    body = head + payload
    checksum = crc32(body) if crc is None else crc
    return body + struct.pack(">I", checksum)


def parse_op(op: str, args) -> bytes:
    if op == "echo":
        return bytes([OP_ECHO]) + args.text.encode()
    if op == "upper":
        return bytes([OP_UPPER]) + args.text.encode()
    if op == "slow":
        return bytes([OP_SLOW]) + struct.pack(">I", args.delay) + args.text.encode()
    if op == "add":
        return bytes([OP_ADD]) + struct.pack(">QQ", args.num[0], args.num[1])
    if op == "fail":
        return bytes([OP_FAIL])
    raise SystemExit(f"unknown op {op}")


def annotate(frame: bytes) -> str:
    if len(frame) < HEADER_LEN + TRAILER_LEN:
        return f"<short frame: {frame.hex()}>"
    magic, version, flags, command = frame[0:2], frame[2], frame[3], frame[4]
    rid, plen = struct.unpack(">II", frame[5:13])
    payload = frame[13:13 + plen]
    crc, = struct.unpack(">I", frame[13 + plen:17 + plen])
    calc = crc32(frame[:13 + plen])
    lines = [
        f"  magic       {magic.hex(' ')}  ({'OK' if magic == MAGIC else 'BAD'})",
        f"  version     {version}",
        f"  flags       x{flags:02x}",
        f"  command     {command} ({CMD_NAME.get(command, 'UNKNOWN')})",
        f"  request_id  {rid}",
        f"  payload_len {plen}",
        f"  payload     {payload.hex(' ')}  ({len(payload)} bytes)",
        f"  crc32       {crc:08x} ({'matches' if crc == calc else f'MISMATCH computed {calc:08x}'})",
        f"  total       {len(frame)} bytes",
    ]
    return "\n".join(lines)


class FrameReader:
    """Incremental decoder mirroring the Rust semantics, in Python."""

    def __init__(self, max_payload=1 << 20, byte_at_a_time=False, sock=None):
        self.buf = bytearray()
        self.max_payload = max_payload
        self.byte_at_a_time = byte_at_a_time
        self.sock = sock

    def _fill(self, want):
        while len(self.buf) < want:
            if self.byte_at_a_time:
                chunk = self.sock.recv(1)
            else:
                chunk = self.sock.recv(65536)
            if not chunk:
                return False
            self.buf.extend(chunk)
        return True

    def read_frame(self):
        if not self._fill(HEADER_LEN):
            return None
        if self.buf[0:2] != MAGIC:
            raise ValueError(f"bad magic {bytes(self.buf[0:2]).hex()}")
        version = self.buf[2]
        if version != VERSION:
            raise ValueError(f"unsupported version {version}")
        command = self.buf[4]
        rid, plen = struct.unpack(">II", bytes(self.buf[5:13]))
        if plen > self.max_payload:
            raise ValueError(f"payload {plen} > limit {self.max_payload}")
        total = HEADER_LEN + plen + TRAILER_LEN
        if not self._fill(total):
            raise ValueError("EOF mid-frame")
        payload = bytes(self.buf[13:13 + plen])
        crc, = struct.unpack(">I", bytes(self.buf[13 + plen:total]))
        if crc != crc32(bytes(self.buf[:13 + plen])):
            raise ValueError("crc mismatch")
        del self.buf[:total]
        return command, rid, payload


def describe_payload(command: int, payload: bytes) -> str:
    if command == CMD["error"]:
        code = struct.unpack(">H", payload[:2])[0] if len(payload) >= 2 else 0
        msg = payload[2:].decode("utf-8", "replace")
        return f"ERROR code={code} ({ERROR_NAMES.get(code, '?')}) text={msg!r}"
    if not payload:
        return "<empty>"
    status = payload[0]
    body = payload[1:]
    if command == CMD["response"]:
        if status == 0:
            if len(body) == 8:
                return f"RESPONSE OK u64={struct.unpack('>Q', body)[0]} raw={body.hex()}"
            return f"RESPONSE OK text={body.decode('utf-8', 'replace')!r} raw={body.hex()}"
        return f"RESPONSE APP_ERROR {body.decode('utf-8', 'replace')!r}"
    return f"raw={payload.hex()}"


def cmd_hexdump(args):
    frame = build_frame(CMD["request"], args.id, parse_op(args.op, args))
    print(f"REQUEST op={args.op} id={args.id}, {len(frame)} bytes:")
    print(frame.hex(" "))
    print(annotate(frame))


def connect(addr):
    host, port = addr.split(":")
    s = socket.create_connection((host, int(port)), timeout=5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def cmd_send(args):
    s = connect(args.addr)
    frame = build_frame(CMD["request"], args.id, parse_op(args.op, args))
    s.sendall(frame)
    reader = FrameReader(sock=s, byte_at_a_time=args.half_reads)
    decoded = reader.read_frame()
    if decoded is None:
        print("server closed without a reply")
        return
    command, rid, payload = decoded
    print(f"reply id={rid}: {describe_payload(command, payload)}")
    s.close()


def cmd_soak(args):
    s = connect(args.addr)
    # Sticky: every REQUEST frame is concatenated then written in one send.
    blob = b"".join(
        build_frame(CMD["request"], 1000 + i, bytes([OP_ECHO]) + f"msg-{i}".encode())
        for i in range(args.n)
    )
    s.sendall(blob)
    # Half packets: read the replies strictly one byte at a time.
    reader = FrameReader(sock=s, byte_at_a_time=True)
    # Server workers run concurrently, so replies may arrive OUT OF ORDER:
    # match each reply by its request id (this is the point of multiplexing).
    got = {}
    for _ in range(args.n):
        command, rid, payload = reader.read_frame()
        assert command == CMD["response"], (command, payload)
        assert rid not in got, f"duplicate reply for id {rid}"
        got[rid] = payload
    for i in range(args.n):
        rid = 1000 + i
        assert rid in got, f"missing reply for {rid}"
        assert got[rid] == b"\x00" + f"msg-{i}".encode(), got[rid]
    in_order = list(got.keys()) == [1000 + i for i in range(args.n)]
    print(
        f"soak OK: {args.n}/{args.n} sticky-sent requests, byte-at-a-time replies; "
        f"all matched by id (arrival {'in id order' if in_order else 'OUT OF ORDER — routed correctly'})"
    )
    s.close()


def read_any(s, timeout=2.0):
    s.settimeout(timeout)
    try:
        data = s.recv(65536)
    except socket.timeout:
        return b""
    return data


def cmd_inject(args):
    kind = args.kind
    s = connect(args.addr)

    if kind == "bad-crc":
        f = build_frame(CMD["ping"], 1, b"")
        broken = bytearray(f)
        broken[-1] ^= 0xFF
        s.sendall(broken)
    elif kind == "bad-version":
        f = build_frame(CMD["ping"], 1, b"", version=9)
        s.sendall(f)
    elif kind == "oversized":
        # Advertise 16 MiB payload limit violation; send header + dummy bytes.
        f = build_frame(CMD["request"], 1, b"", crc=0)
        f = f[:9] + struct.pack(">I", 16 << 20) + b"\x00" * 4
        s.sendall(f)
    elif kind == "unknown-cmd":
        s.sendall(build_frame(77, 1, b""))
    elif kind == "bad-flags":
        s.sendall(build_frame(CMD["ping"], 1, b"", flags=0x80))
    elif kind == "garbage":
        s.sendall(b"\xff" * 64)
    elif kind == "half-frame":
        f = build_frame(CMD["ping"], 1, b"")
        s.sendall(f[:-3])
    elif kind == "dup-id":
        slow = build_frame(CMD["request"], 77,
                          bytes([OP_SLOW]) + struct.pack(">I", 5000))
        dup = build_frame(CMD["request"], 77, bytes([OP_ECHO]) + b"dup")
        s.sendall(slow + dup)
    elif kind == "cancel-unknown":
        s.sendall(build_frame(CMD["cancel"], 4242, b""))
    else:
        raise SystemExit(f"unknown injection {kind}")

    if kind == "half-frame":
        s.close()
        print("half-frame: sent truncated frame then closed (server must drop connection)")
        return

    data = read_any(s)
    if not data:
        print(f"{kind}: server CLOSED the connection (fatal framing error) — expected")
        return
    # Decode one or more error frames.
    import io
    reader = FrameReader(sock=None)
    reader.buf.extend(data)
    count = 0
    while len(reader.buf) >= HEADER_LEN:
        try:
            # reuse parser without socket by monkey-reading from buffer
            saved = bytes(reader.buf)
            if len(saved) < HEADER_LEN:
                break
            rid, plen = struct.unpack(">II", saved[5:13])
            total = HEADER_LEN + plen + TRAILER_LEN
            if len(saved) < total:
                break
            command = saved[4]
            payload = saved[13:13 + plen]
            print(f"{kind}: server replied -> {describe_payload(command, payload)}")
            del reader.buf[:total]
            count += 1
        except ValueError as e:
            print(f"{kind}: decode error {e}")
            break
    if count == 0:
        print(f"{kind}: unparseable reply: {data.hex(' ')}")
    # For recoverable errors, prove the connection still works.
    if kind in ("unknown-cmd", "bad-flags", "dup-id", "cancel-unknown"):
        s.sendall(build_frame(CMD["ping"], 2, b""))
        reader2 = FrameReader(sock=s)
        command, rid, _ = reader2.read_frame()
        assert command == CMD["pong"], "connection did not survive recoverable error"
        print(f"{kind}: follow-up PING -> PONG, connection ALIVE as designed")
    s.close()


def cmd_dump(args):
    data = open(args.file, "rb").read()
    off = 0
    n = 0
    while off < len(data):
        if len(data) - off < HEADER_LEN:
            print(f"trailing {len(data) - off} bytes (half frame): {data[off:].hex()}")
            break
        rid, plen = struct.unpack(">II", data[off + 5:off + 13])
        total = HEADER_LEN + plen + TRAILER_LEN
        frame = data[off:off + total]
        print(f"--- frame {n} at offset {off} ---")
        print(annotate(frame))
        off += total
        n += 1
    print(f"decoded {n} frame(s) from {args.file}")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp):
        sp.add_argument("op", choices=["echo", "upper", "slow", "add", "fail"])
        sp.add_argument("--id", type=int, default=1)
        sp.add_argument("--delay", type=int, default=500)
        sp.add_argument("--text", default="hello")
        sp.add_argument("--num", type=int, nargs=2, default=[40, 2])

    sp = sub.add_parser("hexdump"); add_common(sp); sp.set_defaults(fn=cmd_hexdump)
    sp = sub.add_parser("send"); sp.add_argument("addr"); add_common(sp)
    sp.add_argument("--half-reads", action="store_true"); sp.set_defaults(fn=cmd_send)
    sp = sub.add_parser("soak"); sp.add_argument("addr"); sp.add_argument("--n", type=int, default=50)
    sp.set_defaults(fn=cmd_soak)
    sp = sub.add_parser("inject"); sp.add_argument("addr")
    sp.add_argument("kind", choices=["bad-crc", "bad-version", "oversized", "unknown-cmd",
                                     "bad-flags", "garbage", "half-frame", "dup-id",
                                     "cancel-unknown"])
    sp.set_defaults(fn=cmd_inject)
    sp = sub.add_parser("dump"); sp.add_argument("file"); sp.set_defaults(fn=cmd_dump)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
