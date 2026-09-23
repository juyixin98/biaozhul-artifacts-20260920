#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MurmurHash3 x64 128-bit 的独立参考实现（与 Java 端相互独立，用于交叉验证）。

用途：生成固定哈希算法的已知答案向量 (KAT)，写入
     results/hash-kat-vectors.json，供 Java 端
     dev.dedup.hll.HashAlgorithmTest 核对。

该文件只是验证/数据生成工具，不参与 Java 构建，也不被运行时依赖。
算法为公有领域的 Austin Appleby MurmurHash3 x64_128，
与 Java 端 dev.dedup.hll.Murmur3Hash128 的实现必须字节级一致。
"""
import json
import os
import struct

FIXED_SEED = 0x9747B28C & 0xFFFFFFFF  # 与 Murmur3Hash128.FIXED_SEED 相同
MASK64 = (1 << 64) - 1


def _rotl64(x, r):
    return ((x << r) | (x >> (64 - r))) & MASK64


def _fmix64(k):
    k ^= k >> 33
    k = (k * 0xFF51AFD7ED558CCD) & MASK64
    k ^= k >> 33
    k = (k * 0xC4CEB9FE1A85EC53) & MASK64
    k ^= k >> 33
    return k & MASK64


def murmur3_x64_128(data: bytes, seed: int):
    """返回 (h1, h2)，两个 64 位无符号整数。"""
    length = len(data)
    nblocks = length // 16

    h1 = seed & MASK64
    h2 = seed & MASK64

    c1 = 0x87C37B91114253D5
    c2 = 0x4CF5AD432745937F

    for i in range(nblocks):
        k1 = struct.unpack_from("<Q", data, i * 16)[0]
        k2 = struct.unpack_from("<Q", data, i * 16 + 8)[0]

        k1 = (k1 * c1) & MASK64
        k1 = _rotl64(k1, 31)
        k1 = (k1 * c2) & MASK64
        h1 ^= k1
        h1 = _rotl64(h1, 27)
        h1 = (h1 + h2) & MASK64
        h1 = (h1 * 5 + 0x52DCE729) & MASK64

        k2 = (k2 * c2) & MASK64
        k2 = _rotl64(k2, 33)
        k2 = (k2 * c1) & MASK64
        h2 ^= k2
        h2 = _rotl64(h2, 31)
        h2 = (h2 + h1) & MASK64
        h2 = (h2 * 5 + 0x38495AB5) & MASK64

    tail = data[nblocks * 16:]
    k1 = 0
    k2 = 0
    tail_len = len(tail)
    if tail_len >= 15:
        k2 ^= tail[14] << 48
    if tail_len >= 14:
        k2 ^= tail[13] << 40
    if tail_len >= 13:
        k2 ^= tail[12] << 32
    if tail_len >= 12:
        k2 ^= tail[11] << 24
    if tail_len >= 11:
        k2 ^= tail[10] << 16
    if tail_len >= 10:
        k2 ^= tail[9] << 8
    if tail_len >= 9:
        k2 ^= tail[8]
        k2 = (k2 * c2) & MASK64
        k2 = _rotl64(k2, 33)
        k2 = (k2 * c1) & MASK64
        h2 ^= k2

    if tail_len >= 8:
        k1 ^= tail[7] << 56
    if tail_len >= 7:
        k1 ^= tail[6] << 48
    if tail_len >= 6:
        k1 ^= tail[5] << 40
    if tail_len >= 5:
        k1 ^= tail[4] << 32
    if tail_len >= 4:
        k1 ^= tail[3] << 24
    if tail_len >= 3:
        k1 ^= tail[2] << 16
    if tail_len >= 2:
        k1 ^= tail[1] << 8
    if tail_len >= 1:
        k1 ^= tail[0]
        k1 = (k1 * c1) & MASK64
        k1 = _rotl64(k1, 31)
        k1 = (k1 * c2) & MASK64
        h1 ^= k1

    h1 ^= length
    h2 ^= length

    h1 = (h1 + h2) & MASK64
    h2 = (h2 + h1) & MASK64
    h1 = _fmix64(h1)
    h2 = _fmix64(h2)
    h1 = (h1 + h2) & MASK64
    h2 = (h2 + h1) & MASK64
    return h1, h2


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    cases = [b"", b"a", b"abc", b"hello", b"hello world",
             b"The quick brown fox jumps over the lazy dog",
             bytes(range(256)), b"\x00" * 100]
    vectors = []
    for raw in cases:
        h1, h2 = murmur3_x64_128(raw, FIXED_SEED)
        vectors.append({
            "inputUtf8Base64": __import__("base64").b64encode(raw).decode(),
            "length": len(raw),
            "h1Hex": "%016x" % h1,
            "h2Hex": "%016x" % h2,
        })
    # UTF-8 字符串用例（中文，验证编码一致性）
    for s in ["可合并近似去重", "HyperLogLog"]:
        raw = s.encode("utf-8")
        h1, h2 = murmur3_x64_128(raw, FIXED_SEED)
        vectors.append({
            "inputUtf8": s,
            "length": len(raw),
            "h1Hex": "%016x" % h1,
            "h2Hex": "%016x" % h2,
        })
    out = {
        "algorithm": "MurmurHash3_x64_128",
        "seedHex": "0x%08x" % FIXED_SEED,
        "note": "Python 独立参考实现生成；Java 测试 HashAlgorithmTest 必须与之逐向量一致。",
        "vectors": vectors,
    }
    path = os.path.join(here, "hash-kat-vectors.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("wrote %s (%d vectors)" % (path, len(vectors)))
    for v in vectors:
        label = v.get("inputUtf8", v.get("inputUtf8Base64"))
        print(label, v["h1Hex"], v["h2Hex"])


if __name__ == "__main__":
    main()
