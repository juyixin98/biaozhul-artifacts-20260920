"""区组槽位铺设与纯分配函数。

关键分离：
- **比例均衡**由 ``lay_out_slots`` 确定性保证——每个完整区组中各臂槽位数
  严格等于 ``ratio * block_multiple``，与随机性无关；
- **顺序随机**由 :func:`app.core.stream.block_permutation` 的无偏
  Fisher–Yates 决定。

因此"比例对不对"和"随机均不均匀"是两件可分别验证的事。
"""
from __future__ import annotations

from ..contracts import AllocationContract
from .stream import block_permutation


def lay_out_slots(contract: AllocationContract) -> tuple[str, ...]:
    """把臂按契约顺序铺进长度为 block_size 的槽位。

    例：A:A:B = 2:1, multiple=2 → 区组长 6，槽位为
    ``('A','A','A','A','B','B')``。置换只决定入组次序落到哪个槽。
    """
    slots: list[str] = []
    for arm in contract.arms:
        slots.extend([arm.arm_id] * (arm.ratio * contract.block_multiple))
    return tuple(slots)


def assign_at_position(contract: AllocationContract, master_seed: bytes,
                       stratum_key: str, block_index: int,
                       position: int) -> dict:
    """纯函数：给定坐标与入组位置，重算该次分配的全部细节。

    恢复、回放、审计都走这里——不需要保存任何 RNG 中间状态。
    """
    if not (0 <= position < contract.block_size):
        raise ValueError(
            f"position {position} 越界（区组长 {contract.block_size}）"
        )
    perm, locators = block_permutation(
        master_seed, contract.study_id, contract.fingerprint(),
        stratum_key, block_index, contract.block_size,
    )
    slot_arms = lay_out_slots(contract)
    slot = perm[position]
    return {
        "arm": slot_arms[slot],
        "slot": slot,
        "position": position,
        "block_index": block_index,
        "stratum_key": stratum_key,
        "permutation": perm,
        "slot_arms": slot_arms,
        "locators": locators,
        "contract_fingerprint": contract.fingerprint(),
        "block_size": contract.block_size,
    }
