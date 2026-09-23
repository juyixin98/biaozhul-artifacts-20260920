#!/usr/bin/env python3
"""Minimal raw-socket RESP2 demo client (Python stdlib only).

It does NOT use any RESP/Redis library: requests are read as raw bytes from a
file or built here, and can be sent one byte at a time with --per-byte to
demonstrate that the server's incremental parser tolerates arbitrary TCP
segmentation.

Examples:
    python3 scripts/resp_client.py samples/01_ping.resp
    python3 scripts/resp_client.py samples/05_pipeline.resp --per-byte 0.01
    python3 scripts/resp_client.py --ping
"""

import argparse
import socket
import sys
import time


def encode_bulk(b: bytes) -> bytes:
    return b"$" + str(len(b)).encode() + b"\r\n" + b + b"\r\n"


def encode_array(parts: list[bytes]) -> bytes:
    out = b"*" + str(len(parts)).encode() + b"\r\n"
    for p in parts:
        out += encode_bulk(p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sample", nargs="?", help="raw RESP request file")
    ap.add_argument("--addr", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=6379)
    ap.add_argument("--per-byte", type=float, default=0.0,
                    help="delay between bytes (seconds); 0 = one write")
    ap.add_argument("--ping", action="store_true", help="send PING")
    ap.add_argument("--echo", help="ECHO <text> (may contain \\r\\n escapes)")
    args = ap.parse_args()

    if args.sample:
        with open(args.sample, "rb") as f:
            wire = f.read()
    elif args.ping:
        wire = encode_array([b"PING"])
    elif args.echo is not None:
        payload = args.echo.encode().decode("unicode_escape").encode("latin1")
        wire = encode_array([b"ECHO", payload])
    else:
        ap.error("provide a sample file, --ping or --echo")

    with socket.create_connection((args.addr, args.port), timeout=5) as s:
        note = (f"# sending {len(wire)} bytes"
                + (f", one byte every {args.per_byte}s" if args.per_byte else ""))
        print(note, flush=True)
        if args.per_byte > 0:
            for b in wire:
                s.sendall(bytes([b]))
                time.sleep(args.per_byte)
        else:
            s.sendall(wire)
        s.shutdown(socket.SHUT_WR)
        reply = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            reply += chunk

    print(f"# received {len(reply)} bytes", flush=True)
    sys.stdout.buffer.write(reply)
    print()
    print("# hex:")
    print(reply.hex(" "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
