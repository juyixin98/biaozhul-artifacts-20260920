"""可审计随机流：以 HMAC-SHA256 为 PRF 的计数器模式确定性流。

为什么不用 ``random.Random`` / ``numpy.random.default_rng`` 作为*源*：
我们需要每一个随机数都能按 ``(研究, 层, 区组序号, 用途, 抽取序号)``
独立复核——审计员不必顺序重放整条流，就能验证"第 b 个区组的第 k 次
抽取是多少"。计数器模式的 PRF 恰好提供这种可定位性；NumPy Generator
只作为把均匀随机字节变成无偏整数的工具（拒绝采样），其默认流不被使用。

域分离
------
每条流绑定上下文标签 ``rct/v1/block-permutation/...``，不同用途
（区组置换 / Fisher-Yates 内部留位）标签不同，杜绝跨用途流复用。
"""
from __future__ import annotations

import hashlib
import hmac
import struct
from dataclasses import dataclass

# 单次 SHA256 输出 32 字节；拒绝采样按 32 位无符号整数消费。
# 用 32 位块做拒绝采样（上限是 2^32 的整数倍），拒绝概率 < n/2^32，
# 对 n <= ~1200 可忽略。
_UINT32_BOUNDS = 1 << 32
_PERMUTATION_DOMAIN = b"rct/v1/block-permutation"
_TAIL_DOMAIN = b"rct/v1/tail-completion"
_ROLL_DOMAIN = b"rct/v1/roll"


def _derive_block_key(master_seed: bytes, study_id: str, contract_fingerprint: str,
                      stratum_key: str, block_index: int) -> bytes:
    """从主种子派生某 (研究,层,区组) 的区组密钥。

    派生本身是 HKDF-Extract/Expand 风格的两段 HMAC，确保区组间、
    研究间、契约版本间密码学隔离。
    """
    if block_index < 0:
        raise ValueError("block_index 不能为负")
    salt = b"rct/v1/derive/study-stratum-block"
    ikm = master_seed
    extract = hmac.new(salt, ikm, hashlib.sha256).digest()
    info = b"|".join([
        study_id.encode("utf-8"),
        contract_fingerprint.encode("ascii"),
        stratum_key.encode("utf-8"),
        struct.pack(">Q", block_index),
    ])
    return hmac.new(extract, info, hashlib.sha256).digest()


@dataclass(frozen=True)
class StreamLocator:
    """随机数的可定位坐标——审计日志记录它，便可逐数复核。"""

    study_id: str
    contract_fingerprint: str
    stratum_key: str
    block_index: int
    purpose: str          # "block_permutation" | "tail_completion"
    draw_index: int       # 该区组内第几次随机抽取（0 起）

    def as_dict(self) -> dict:
        return {
            "study_id": self.study_id,
            "contract_fingerprint": self.contract_fingerprint,
            "stratum_key": self.stratum_key,
            "block_index": self.block_index,
            "purpose": self.purpose,
            "draw_index": self.draw_index,
        }


class AuditableStream:
    """单个区组的随机流：计数器模式 HMAC，逐 uint32 产出。

    同一定位坐标永远得到同一随机数（纯函数），因此：
    - 重放/恢复不需要保存随机状态；
    - 审计可随机抽查任意坐标。
    """

    __slots__ = ("_key", "_domain", "_counter")

    def __init__(self, block_key: bytes, domain: bytes):
        self._key = block_key
        self._domain = domain
        self._counter = 0

    def next_uint32(self) -> int:
        # 32 字节里每次取 4 字节，一个分组够用 8 次；第 9 次计数器前进。
        block_pos = self._counter // 8
        offset = (self._counter % 8) * 4
        digest = hmac.new(
            self._key,
            self._domain + struct.pack(">Q", block_pos),
            hashlib.sha256,
        ).digest()
        self._counter += 1
        return struct.unpack(">I", digest[offset:offset + 4])[0]

    def randbelow(self, n: int) -> int:
        """无偏均匀整数 [0, n)：经典拒绝采样（n < 2^32）。"""
        if n <= 0:
            raise ValueError("randbelow 需要正整数")
        if n == 1:
            # 仍然消耗一次计数器，保持抽取序号在不同 n 下语义稳定
            self.next_uint32()
            return 0
        limit = _UINT32_BOUNDS - (_UINT32_BOUNDS % n)
        while True:
            x = self.next_uint32()
            if x < limit:
                return x % n


def block_permutation(master_seed: bytes, study_id: str, contract_fingerprint: str,
                      stratum_key: str, block_index: int, block_size: int,
                      ) -> tuple[tuple[int, ...], list[StreamLocator]]:
    """生成一个完整区组的槽位置换。

    返回 ``(置换, 每个随机决定的定位坐标)``。置换是 0..block_size-1 的排列；
    调用方按契约把臂标签铺进槽位（见 :mod:`app.core.allocator`），
    因此"比例正确"由槽位铺设确定性保证，与随机性无关——随机性只决定
    顺序。这是把*均衡*与*随机*两个关注点分离的关键。
    """
    if block_size < 1:
        raise ValueError("block_size 必须为正")
    block_key = _derive_block_key(
        master_seed, study_id, contract_fingerprint, stratum_key, block_index
    )
    stream = AuditableStream(block_key, _PERMUTATION_DOMAIN)
    perm = list(range(block_size))
    locators: list[StreamLocator] = []
    # 无偏 Fisher–Yates（从全排列上均匀采样，block_size-1 次抽取）
    for i in range(block_size - 1, 0, -1):
        j = stream.randbelow(i + 1)
        locators.append(StreamLocator(
            study_id=study_id,
            contract_fingerprint=contract_fingerprint,
            stratum_key=stratum_key,
            block_index=block_index,
            purpose="block_permutation",
            draw_index=block_size - 1 - i,
        ))
        perm[i], perm[j] = perm[j], perm[i]
    return tuple(perm), locators


def tail_completion_pick(master_seed: bytes, study_id: str, contract_fingerprint: str,
                        stratum_key: str, block_index: int, draw_index: int,
                        candidate_slots: list[int]) -> int:
    """尾组"按比例选下一槽"用的独立随机数（keep_open 模式登记未满区组）。

    与完整区组置换域分离；返回候选槽位下标。此函数当前编排层统一使用
    "入组即固化全置换"策略，尾组同样在开组时确定完整置换，所以本函数
    保留用于证据侧演示与未来在线策略，属于已测试的工具函数。
    """
    block_key = _derive_block_key(
        master_seed, study_id, contract_fingerprint, stratum_key, block_index
    )
    stream = AuditableStream(block_key, _TAIL_DOMAIN)
    for _ in range(draw_index + 1):
        idx = stream.randbelow(len(candidate_slots))
    return candidate_slots[idx]


def locate_uint32(master_seed: bytes, locator: StreamLocator) -> int:
    """审计复核：按定位坐标独立重算某个 uint32（计数器按抽取序号推进）。"""
    block_key = _derive_block_key(
        master_seed, locator.study_id, locator.contract_fingerprint,
        locator.stratum_key, locator.block_index,
    )
    domain = {
        "block_permutation": _PERMUTATION_DOMAIN,
        "tail_completion": _TAIL_DOMAIN,
        "roll": _ROLL_DOMAIN,
    }[locator.purpose]
    stream = AuditableStream(block_key, domain)
    value = 0
    for _ in range(locator.draw_index + 1):
        value = stream.next_uint32()
    return value
