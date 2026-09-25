#!/usr/bin/env python3
"""逐字节比对 ipfrag-client 的响应与独立复算的原始载荷。

用法: _compare_payload.py <responses_file> <seed> <length>

从 responses_file 中找到 "event":"completed" 的响应行，
用与 fraggen 完全一致的 xorshift64* 独立重放 length 字节，
逐字节比较 payload_hex，并核对 total_bytes / sha256。
"""
import hashlib
import json
import sys

MASK = (1 << 64) - 1
C = 0x2545F4914F6CDD1D


def fraggen_payload(seed: int, length: int) -> bytes:
    state = seed | 1
    out = bytearray()
    for _ in range(length):
        x = state
        x = (x ^ (x >> 12)) & MASK
        x = (x ^ ((x << 25) & MASK)) & MASK
        x = (x ^ (x >> 27)) & MASK
        state = x  # 与 Rust `self.0 = x` 一致（乘法前）
        ret = (x * C) & MASK
        out.append(ret & 0xFF)
    return bytes(out)


def main() -> int:
    responses_path, seed, length = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    original = fraggen_payload(seed, length)

    completed = None
    with open(responses_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if obj.get("event") == "completed":
                completed = obj

    if completed is None:
        print("FAIL: no completed event found in responses")
        return 1

    total = completed["total_bytes"]
    returned = bytes.fromhex(completed["payload_hex"])
    server_sha = completed["sha256"]
    expected_sha = hashlib.sha256(original).hexdigest()

    print(f"  total_bytes  server={total} expected={len(original)}")
    print(f"  sha256       server={server_sha}")
    print(f"               expect={expected_sha}")

    ok = True
    if total != len(original):
        print("FAIL: length mismatch")
        ok = False
    if returned != original:
        print("FAIL: payload byte mismatch")
        ok = False
    if server_sha != expected_sha:
        print("FAIL: sha256 mismatch")
        ok = False
    if ok:
        print("  MATCH: reassembled datagram is byte-identical to original payload")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
