"""独立参考实现（仅依赖 Python 标准库 hashlib/hmac/struct）。

这个模块**不导入 app 的任何代码**，用另一份独立写法重新实现同一统计
机制，供测试交叉核对。如果被测内核与本参考同时给出相同的逐位置结果，
"答案抄被测代码"的风险就被排除：两边是独立推导的。

机制规格（与 README 一致）：
- 区组密钥：extract = HMAC_SHA256(salt, master_seed)；
  block_key = HMAC_SHA256(extract, study_id|fp|stratum|block_index(8B大端))
- 区组随机流：HMAC(key, b"rct/v1/block-permutation" || counter(8B大端))，
  每次 SHA256 输出按 4 字节大端提供 8 个 uint32；
- randbelow(n)：limit=2^32-(2^32 mod n) 的拒绝采样；
- Fisher–Yates：i 从 k-1 递减到 1，j=randbelow(i+1)，交换 perm[i]/perm[j]；
- 槽位：按臂声明顺序重复 ratio*multiple，position 的臂=slots[perm[position]]。
"""
from __future__ import annotations

import hashlib
import hmac
import itertools
import struct

SALT = b"rct/v1/derive/study-stratum-block"
DOMAIN = b"rct/v1/block-permutation"
U32 = 1 << 32


def derive_block_key(master_seed: bytes, study_id: str,
                     contract_fingerprint: str, stratum_key: str,
                     block_index: int) -> bytes:
    extract = hmac.new(SALT, master_seed, hashlib.sha256).digest()
    info = b"|".join([
        study_id.encode(), contract_fingerprint.encode(),
        stratum_key.encode(), struct.pack(">Q", block_index),
    ])
    return hmac.new(extract, info, hashlib.sha256).digest()


class _RefStream:
    def __init__(self, key: bytes):
        self.key = key
        self.counter = 0

    def uint32(self) -> int:
        block_pos = self.counter // 8
        offset = (self.counter % 8) * 4
        digest = hmac.new(self.key, DOMAIN + struct.pack(">Q", block_pos),
                          hashlib.sha256).digest()
        self.counter += 1
        return struct.unpack(">I", digest[offset:offset + 4])[0]

    def randbelow(self, n: int) -> int:
        if n == 1:
            self.uint32()
            return 0
        limit = U32 - (U32 % n)
        while True:
            x = self.uint32()
            if x < limit:
                return x % n


def ref_permutation(master_seed: bytes, study_id: str,
                    contract_fingerprint: str, stratum_key: str,
                    block_index: int, block_size: int) -> tuple[int, ...]:
    key = derive_block_key(master_seed, study_id, contract_fingerprint,
                           stratum_key, block_index)
    stream = _RefStream(key)
    perm = list(range(block_size))
    for i in range(block_size - 1, 0, -1):
        j = stream.randbelow(i + 1)
        perm[i], perm[j] = perm[j], perm[i]
    return tuple(perm)


def ref_slot_arms(arm_ratios: list[tuple[str, int]], block_multiple: int
                  ) -> tuple[str, ...]:
    return tuple(arm for arm_id, ratio in arm_ratios
                 for arm in [arm_id] * (ratio * block_multiple))


def ref_assign(master_seed: bytes, study_id: str, contract_fingerprint: str,
               stratum_key: str, block_index: int,
               arm_ratios: list[tuple[str, int]], block_multiple: int
               ) -> list[str]:
    """返回一个完整区组的入组顺序臂别（position 0..block_size-1）。"""
    slots = ref_slot_arms(arm_ratios, block_multiple)
    perm = ref_permutation(master_seed, study_id, contract_fingerprint,
                           stratum_key, block_index, len(slots))
    return [slots[p] for p in perm]


def all_permutations(k: int) -> list[tuple[int, ...]]:
    return list(itertools.permutations(range(k)))
