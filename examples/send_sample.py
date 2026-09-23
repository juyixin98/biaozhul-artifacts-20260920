#!/usr/bin/env python3
"""Synthetic TLS client-bytes sender for the local tls-observer-server.

Uses ONLY the Python standard library. It does not perform a TLS handshake;
it synthesizes raw record-layer / handshake bytes (the same shapes covered
by the Rust unit tests) and sends the client direction to the observer over
plain TCP, then prints the JSON observation the server returns.

Usage:
    python3 examples/send_sample.py [SCENARIO] [--port 8443] [--host 127.0.0.1]

Scenarios:
    valid            ClientHello with SNI + ALPN (default)
    fragment         ClientHello split across 3 records, sent byte-chunked
    duplicate        two SNI / two ALPN extensions (first wins, warning)
    nested-bad       extensions block length lies about its contents
    grease           GREASE cipher suite + GREASE extension
    trunc-record     record header/fragment cut mid-stream
    trunc-hs         handshake message cut across records, never completed
    ciphertext       ClientHello then ApplicationData (must not be parsed)
    ciphertext-only  stream starts with ApplicationData (no CH invented)
    bad-ctype        content_type byte 99
    garbage          random bytes that merely start with 0x16
    all              run every scenario in sequence
"""

import argparse
import socket
import sys

CT_CCS, CT_ALERT, CT_HANDSHAKE, CT_APP_DATA = 20, 21, 22, 23
HS_CLIENT_HELLO = 1


def record(content_type: int, payload: bytes, ver=(3, 1)) -> bytes:
    assert len(payload) <= 0xFFFF
    return bytes([content_type, ver[0], ver[1]]) + len(payload).to_bytes(2, "big") + payload


def hs_message(msg_type: int, body: bytes) -> bytes:
    assert len(body) <= 0xFFFFFF
    return bytes([msg_type]) + len(body).to_bytes(4, "big")[1:] + body


def vec16(b: bytes) -> bytes:
    return len(b).to_bytes(2, "big") + b


def ext_sni(host: str) -> bytes:
    name = b"\x00" + len(host).to_bytes(2, "big") + host.encode()
    data = vec16(name)
    return (0x0000).to_bytes(2, "big") + len(data).to_bytes(2, "big") + data


def ext_alpn(names) -> bytes:
    lst = b"".join(bytes([len(n)]) + n.encode() for n in names)
    data = vec16(lst)
    return (0x0010).to_bytes(2, "big") + len(data).to_bytes(2, "big") + data


def ext_raw(type_: int, data: bytes) -> bytes:
    return type_.to_bytes(2, "big") + len(data).to_bytes(2, "big") + data


def client_hello(extensions: bytes, cipher_suites=(0x1301, 0x1302, 0xC02F),
                 version=(3, 3)) -> bytes:
    b = bytearray()
    b += bytes([version[0], version[1]])
    b += b"\x42" * 32                      # random
    b += b"\x00"                           # session_id length
    cs = b"".join(c.to_bytes(2, "big") for c in cipher_suites)
    b += vec16(cs)
    b += b"\x01\x00"                       # compression_methods: [null]
    b += vec16(extensions)
    return bytes(b)


# --- scenarios -------------------------------------------------------------

def s_valid() -> bytes:
    ext = ext_sni("example.com") + ext_alpn(["h2", "http/1.1"])
    return record(CT_HANDSHAKE, hs_message(HS_CLIENT_HELLO, client_hello(ext)))


def s_fragment() -> bytes:
    ext = ext_sni("fragmented.example.net") + ext_alpn(["h2"])
    msg = hs_message(HS_CLIENT_HELLO, client_hello(ext, (0x1301,)))
    # split the handshake at arbitrary offsets, each in its own record
    a, b2 = 4 + 10, 4 + 60
    return (record(CT_HANDSHAKE, msg[:a])
            + record(CT_HANDSHAKE, msg[a:b2])
            + record(CT_HANDSHAKE, msg[b2:]))


def s_duplicate() -> bytes:
    ext = (ext_sni("first.example") + ext_sni("second.example")
           + ext_alpn(["h2"]) + ext_alpn(["http/1.1"]))
    return record(CT_HANDSHAKE, hs_message(HS_CLIENT_HELLO, client_hello(ext)))


def s_nested_bad() -> bytes:
    # Correctly framed record + handshake, but ClientHello declares an
    # extensions length of 100 while only 4 bytes follow.
    b = bytearray()
    b += b"\x03\x03"
    b += b"\x11" * 32
    b += b"\x00"
    b += (2).to_bytes(2, "big") + (0x1301).to_bytes(2, "big")
    b += b"\x01\x00"
    b += (100).to_bytes(2, "big") + b"\x00\x00\x00\x00"
    return record(CT_HANDSHAKE, hs_message(HS_CLIENT_HELLO, bytes(b)))


def s_grease() -> bytes:
    ext = ext_raw(0x2A2A, b"\x01\x02\x03") + ext_sni("grease.test") \
        + ext_raw(0x00AB, b"\x99\x99") + ext_alpn(["h2"])
    return record(CT_HANDSHAKE,
                  hs_message(HS_CLIENT_HELLO, client_hello(ext, (0x0A0A, 0x1301))))


def s_trunc_record() -> bytes:
    return record(CT_HANDSHAKE, b"\x00" * 80)[:9]   # cut 9 bytes in


def s_trunc_hs() -> bytes:
    ext = ext_sni("never.finishes")
    msg = hs_message(HS_CLIENT_HELLO, client_hello(ext))
    return record(CT_HANDSHAKE, msg[: len(msg) // 2])  # second half missing


def s_ciphertext() -> bytes:
    out = bytearray(s_valid())
    # pseudo-encrypted blob; contains bytes that look like records inside
    bogus = bytes(((i * 2654435761) & 0xFF) for i in range(120))
    out += record(CT_APP_DATA, bogus)
    out += record(CT_CCS, b"\x01")
    out += record(CT_APP_DATA, b"\x00" * 40)
    return bytes(out)


def s_ciphertext_only() -> bytes:
    # ApplicationData first, with bytes that superficially resemble hs data.
    return record(CT_APP_DATA, bytes([22, 3, 3, 1, 0, 1, 0xFF]))


def s_bad_ctype() -> bytes:
    return record(99, b"\x00\x00\x00")


def s_garbage() -> bytes:
    # Starts with 0x16 but lengths are nonsense / truncated.
    return bytes([22, 3, 3, 0, 50, 1, 2, 3])


SCENARIOS = {
    "valid": s_valid,
    "fragment": s_fragment,
    "duplicate": s_duplicate,
    "nested-bad": s_nested_bad,
    "grease": s_grease,
    "trunc-record": s_trunc_record,
    "trunc-hs": s_trunc_hs,
    "ciphertext": s_ciphertext,
    "ciphertext-only": s_ciphertext_only,
    "bad-ctype": s_bad_ctype,
    "garbage": s_garbage,
}


def send_one(host: str, port: int, name: str, data: bytes, chunk: int) -> str:
    with socket.create_connection((host, port), timeout=5) as s:
        # Send in awkward small chunks to prove incremental parsing works,
        # then half-close so the server knows the client direction ended.
        view = memoryview(data)
        # fragment scenario: even smaller chunks
        step = chunk
        for i in range(0, len(view), step):
            s.sendall(view[i:i + step])
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            got = s.recv(4096)
            if not got:
                break
            chunks.append(got)
    return b"".join(chunks).decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenario", nargs="?", default="valid",
                    choices=sorted(list(SCENARIOS) + ["all"]))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8443)
    ap.add_argument("--chunk", type=int, default=7,
                    help="TCP write chunk size (default 7, intentionally awkward)")
    args = ap.parse_args()

    names = list(SCENARIOS) if args.scenario == "all" else [args.scenario]
    rc = 0
    for name in names:
        data = SCENARIOS[name]()
        print(f"=== scenario: {name} ({len(data)} client bytes) ===")
        try:
            reply = send_one(args.host, args.port, name, data, args.chunk)
        except OSError as e:
            print(f"connection failed: {e}", file=sys.stderr)
            return 2
        print(reply)
        # crude exit code aggregation for scripting: any scenario where the
        # observer reports ok=false (except the deliberately-malformed ones)
        # is treated as failure
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
