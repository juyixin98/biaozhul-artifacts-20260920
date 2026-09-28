"""随机分布核验：多固定种子扫描 + SciPy 检验。

统计纪律（重要）
----------------
这里的检验**不能"证明"随机性正确**。固定种子是确定性算法，无所谓随机；
我们检验的是"算法在大量固定种子上的输出分布是否与理论分布一致"，
结论只有两种：

- ``未检出偏倚（no departure detected）``：p 值不显著 → 不确定性保留，
  不是正确性证明；
- ``检出偏离（departure detected）``：p 值显著 → 机制可能有误，需排查。

正确性的硬证据来自 :mod:`app.evidence.stream_verify` 的逐数复算与
:mod:`app.evidence.balance` 的区组枚举，而不是 p 值。
"""
from __future__ import annotations

import math
from typing import Any

import numpy as np
from scipy import stats

from ..core.stream import block_permutation

# 验收用固定种子（base64 解码后 32 字节）。显式列在代码里，报告中逐字记录，
# 任何人都能逐位复算；它们不是秘密——主种子的*保密性*与*可复现性*无关，
# 本合成实验选择公开测试种子以便审计。
FIXED_SEEDS_B64: tuple[str, ...] = (
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",  # 32 零字节
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAE=",  # 末字节 1
    "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MGFiY2RlZmdoaWo=",  # 固定文本派生
    "MTExMTExMTExMTExMTExMTExMTExMTExMTExMTExMTE=",  # 31 个 '1'
    "KioqKioqKioqKioqKioqKioqKioqKioqKioqKioqKio=",  # 32 个 '*'
    "dGVzdC1zZWVkLWRvLW5vdC11c2UtaW4tLXByb2R1Y3Q=",  # 固定文本
)

ALPHA = 0.01  # 对多检验更保守；结论仍只是"未检出"


def _decode_seeds(seeds_b64: tuple[str, ...]) -> list[bytes]:
    import base64
    return [base64.b64decode(s, validate=True) for s in seeds_b64]


def enumerate_permutation_distribution(block_size: int, *,
                                       seeds_b64: tuple[str, ...] = FIXED_SEEDS_B64,
                                       blocks_per_seed: int = 200,
                                       alpha: float = ALPHA
                                       ) -> dict[str, Any]:
    """小区组全排列枚举检验（block_size <= 5 时覆盖全部 K! 个置换）。

    对每个固定种子取 ``blocks_per_seed`` 个不同区组序号的置换，统计
    K! 种置换的出现频次，做卡方拟合优度检验（原假设：均匀）。
    """
    if not 2 <= block_size <= 5:
        raise ValueError("全排列枚举仅支持 block_size 2..5")
    seeds = _decode_seeds(seeds_b64)
    k_fact = math.factorial(block_size)
    counts = {p: 0 for p in _all_permutations(block_size)}
    index: dict[tuple[int, ...], int] = {p: i for i, p in enumerate(counts)}

    draws_meta: list[dict] = []
    for si, seed in enumerate(seeds):
        for bi in range(blocks_per_seed):
            perm, _ = block_permutation(
                seed, "dist-probe", "ctr_enumeration", "S0", bi, block_size
            )
            counts[perm] += 1
            draws_meta.append({
                "seed_index": si,
                "seed_b64": seeds_b64[si],
                "block_index": bi,
                "permutation": list(perm),
            })

    observed = np.array(list(counts.values()), dtype=float)
    n = int(observed.sum())
    expected = np.full(k_fact, n / k_fact)
    chi2, p_value = stats.chisquare(observed, expected, ddof=0)

    # 同时给出每个置换的经验频率与 1/K!，供肉眼核对
    table = [
        {"permutation": list(p), "count": int(c),
         "frequency": round(c / n, 6), "expected_frequency": round(1 / k_fact, 6)}
        for p, c in counts.items()
    ]
    return {
        "test": "permutation_uniformity_chisquare",
        "block_size": block_size,
        "possible_permutations": k_fact,
        "n_draws": n,
        "seeds": list(seeds_b64),
        "blocks_per_seed": blocks_per_seed,
        "statistic_chi2": float(chi2),
        "p_value": float(p_value),
        "alpha": alpha,
        "departure_detected": bool(p_value < alpha),
        "conclusion": (
            "检出偏离均匀分布，需排查随机机制"
            if p_value < alpha
            else "未检出偏离均匀分布（不等于证明均匀，仅本样本未发现反例）"
        ),
        "permutation_table": table,
    }


def arm_frequency_by_position(arm_ids: tuple[str, ...], slot_arms: tuple[str, ...],
                              block_size: int, *,
                              seeds_b64: tuple[str, ...] = FIXED_SEEDS_B64,
                              blocks_per_seed: int = 400,
                              alpha: float = ALPHA) -> dict[str, Any]:
    """逐槽位臂频率卡方检验。

    每个入组位置上各臂概率应等于其契约比例；对每个位置做一次
    卡方拟合优度检验（多重检验以 alpha 控制，报告全部 p 值）。
    """
    seeds = _decode_seeds(seeds_b64)
    target = np.array([slot_arms.count(a) / block_size for a in arm_ids])
    counts = np.zeros((block_size, len(arm_ids)), dtype=int)

    for seed in seeds:
        for bi in range(blocks_per_seed):
            perm, _ = block_permutation(
                seed, "dist-probe", "ctr_positions", "S0", bi, block_size
            )
            for position, slot in enumerate(perm):
                counts[position, arm_ids.index(slot_arms[slot])] += 1

    n_per_position = counts.sum(axis=1)
    position_results = []
    any_departure = False
    for position in range(block_size):
        expected = target * n_per_position[position]
        chi2, p_value = stats.chisquare(counts[position], expected, ddof=0)
        dep = bool(p_value < alpha)
        any_departure = any_departure or dep
        position_results.append({
            "position": position,
            "counts": {a: int(counts[position, i]) for i, a in enumerate(arm_ids)},
            "expected_proportions": {
                a: float(target[i]) for i, a in enumerate(arm_ids)
            },
            "statistic_chi2": float(chi2),
            "p_value": float(p_value),
            "departure_detected": dep,
        })

    # 序列独立性：把每个区组第 0 个位置的臂看作序列，做游程检验
    runs = _runs_test_first_slot(seeds, arm_ids, slot_arms, alpha)

    return {
        "test": "arm_frequency_per_position_chisquare",
        "arms": list(arm_ids),
        "target_proportions": {a: float(target[i]) for i, a in enumerate(arm_ids)},
        "block_size": block_size,
        "n_blocks": len(seeds) * blocks_per_seed,
        "seeds": list(seeds_b64),
        "positions": position_results,
        "serial_independence_runs_test": runs,
        "alpha": alpha,
        "departure_detected": any_departure or runs["departure_detected"],
        "conclusion": (
            "至少一个位置检出比例偏离或序列相关，需排查"
            if any_departure or runs["departure_detected"]
            else "未检出比例偏离或序列相关（保留不确定性，非正确性证明）"
        ),
    }


def _runs_test_first_slot(seeds, arm_ids, slot_arms, alpha) -> dict[str, Any]:
    """跨区组首槽臂别的游程检验（Wald–Wolfowitz 正态近似）。"""
    sequence: list[int] = []
    for si, seed in enumerate(seeds):
        for bi in range(400):
            perm, _ = block_permutation(
                seed, "dist-probe", "ctr_positions", "S0", bi, len(slot_arms)
            )
            sequence.append(arm_ids.index(slot_arms[perm[0]]))
    arr = np.array(sequence)
    # 二臂时游程检验直接解释；多臂时按"是否等于最常见臂"二分化并标注
    if len(arm_ids) == 2:
        binary = arr
        label_map = {"0": arm_ids[0], "1": arm_ids[1]}
    else:
        mode = int(np.bincount(arr).argmax())
        binary = (arr == mode).astype(int)
        label_map = {"0": f"not-{arm_ids[mode]}", "1": arm_ids[mode]}
    n1 = int(binary.sum())
    n0 = int(len(binary) - n1)
    runs = 1 + int(np.sum(binary[1:] != binary[:-1]))
    expected_runs = 1 + 2 * n0 * n1 / (n0 + n1)
    var_runs = (2 * n0 * n1 * (2 * n0 * n1 - n0 - n1)
                / ((n0 + n1) ** 2 * (n0 + n1 - 1)))
    z = (runs - expected_runs) / math.sqrt(var_runs)
    p_value = float(2 * (1 - stats.norm.cdf(abs(z))))
    return {
        "test": "runs_test_on_first_slot",
        "n": len(binary),
        "label_map": label_map,
        "runs": runs,
        "expected_runs": round(expected_runs, 3),
        "z": round(float(z), 4),
        "p_value": p_value,
        "alpha": alpha,
        "departure_detected": bool(p_value < alpha),
        "note": "多臂研究按最常见臂二分化，仅作粗筛",
    }


def _all_permutations(k: int) -> list[tuple[int, ...]]:
    import itertools
    return list(itertools.permutations(range(k)))
