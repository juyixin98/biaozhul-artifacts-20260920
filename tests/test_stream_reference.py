"""随机内核 vs 独立参考实现（tests/reference.py，仅 hashlib）。

期望值是事先用独立脚本算出并*冻结*在本文件里的常量，测试运行时不会调用
被测代码来"生成答案"。被测契约指纹也必须等于独立计算的指纹——这同时
交叉验证了指纹的规范序列化。
"""
from __future__ import annotations

import pytest

from app.core.allocator import assign_at_position, lay_out_slots
from app.core.stream import block_permutation, locate_uint32, StreamLocator
from tests import reference as ref
from tests.conftest import SEED_A, SEED_B

# ---- 冻结期望（由 tests/reference.py + 规范 JSON 独立预算） ----
EXPECTED_SEED_A_FP = "seed_969e98ab0b5ce312a32a775bce44c3bd5145b1093d11477178f130880cacae66"
TV_FP = "ctr_36739de0821532ddcaf3d1500de5c1b22f54d9e3a638eb122e63a33caedc295f"
EXPECTED_TV_BLOCKS = {
    ("C1|early", 0): {
        "perm": (3, 1, 0, 2),
        "arms": ["treatment", "control", "control", "treatment"],
    },
    ("C1|early", 1): {
        "perm": (2, 3, 0, 1),
        "arms": ["treatment", "treatment", "control", "control"],
    },
    ("C2|late", 0): {
        "perm": (2, 0, 1, 3),
        "arms": ["treatment", "control", "control", "treatment"],
    },
    ("C2|late", 1): {
        "perm": (0, 2, 3, 1),
        "arms": ["control", "treatment", "treatment", "control"],
    },
}

EXPECTED_SEED_B_FP = "seed_7f0e5015b9bf5540b07275ff0cb6fcb18c54abb86567a59ec9aa71a727cb4a0d"
RATIO_FP = "ctr_e824e672a06a9e6e871a005a3a2a6bc95e1e55f0c0e94cb61e3f43fbc6fecb90"
EXPECTED_RATIO_N_BLOCK0 = ["B", "B", "A", "A", "B", "B"]


def test_production_contract_fingerprint_matches_independent(runtime):
    from tests.conftest import make_two_arm_study
    info = make_two_arm_study(runtime["service"])
    assert info["contract_fingerprint"] == TV_FP  # 与独立脚本一致
    assert info["block_size"] == 4


@pytest.mark.parametrize("stratum_key,block_index",
                         list(EXPECTED_TV_BLOCKS.keys()))
def test_permutation_matches_reference(stratum_key, block_index):
    arm_ratios = [("control", 1), ("treatment", 1)]
    prod_perm, _ = block_permutation(
        SEED_A, "TV-STUDY", TV_FP, stratum_key, block_index, 4)
    ref_perm = ref.ref_permutation(
        SEED_A, "TV-STUDY", TV_FP, stratum_key, block_index, 4)
    assert prod_perm == ref_perm
    assert tuple(prod_perm) == EXPECTED_TV_BLOCKS[(stratum_key, block_index)]["perm"]


def test_position_arms_match_frozen_vectors(runtime):
    """端到端：同一层按序登记 8 个对象，逐位置臂别等于冻结向量。"""
    from tests.conftest import make_two_arm_study
    svc = runtime["service"]
    make_two_arm_study(svc)
    for bi in range(2):
        for pos in range(4):
            idx = bi * 4 + pos
            out = svc.allocate(
                study_id="TV-STUDY", subject_id=f"E{idx:02d}",
                features={"center": "C1", "stage": "early"},
                idempotency_key=None, actor_role="investigator",
                request_id=f"req-{idx}")
            assert out["block_index"] == bi
            assert out["position"] == pos
            assert out["arm"] == EXPECTED_TV_BLOCKS[("C1|early", bi)]["arms"][pos]


def test_unequal_ratio_block_layout_and_vector():
    arm_ratios = [("A", 1), ("B", 2)]
    # 槽位铺设确定性保证 2 个 A、4 个 B
    # 直接用契约对象验证比例
    from app.contracts import build_contract
    c = build_contract(
        study_id="TV-RATIO", arm_specs=arm_ratios,
        factor_specs=[("region", ["N", "S"])], block_multiple=2,
        tail_policy="keep_open", seed_fingerprint=EXPECTED_SEED_B_FP,
        seed_proof="pbkdf2-sha256$1$AA$AA")
    assert c.fingerprint() == RATIO_FP
    assert lay_out_slots(c).count("A") == 2
    assert lay_out_slots(c).count("B") == 4
    prod_seq = [
        assign_at_position(c, SEED_B, "N", 0, pos)["arm"]
        for pos in range(6)
    ]
    assert prod_seq == EXPECTED_RATIO_N_BLOCK0
    assert prod_seq == ref.ref_assign(
        SEED_B, "TV-RATIO", RATIO_FP, "N", 0, arm_ratios, 2)


def test_locator_is_pure_and_recomputable():
    """同一定位坐标两次重算必须相同；换区组号必须改变（流隔离）。"""
    loc = StreamLocator(
        study_id="TV-STUDY", contract_fingerprint=TV_FP,
        stratum_key="C1|early", block_index=0,
        purpose="block_permutation", draw_index=0)
    v1 = locate_uint32(SEED_A, loc)
    v2 = locate_uint32(SEED_A, loc)
    assert v1 == v2 and 0 <= v1 < 2 ** 32
    loc_other_block = StreamLocator(
        study_id="TV-STUDY", contract_fingerprint=TV_FP,
        stratum_key="C1|early", block_index=1,
        purpose="block_permutation", draw_index=0)
    assert locate_uint32(SEED_A, loc_other_block) != v1
    loc_other_purpose = StreamLocator(
        study_id="TV-STUDY", contract_fingerprint=TV_FP,
        stratum_key="C1|early", block_index=0,
        purpose="tail_completion", draw_index=0)
    assert locate_uint32(SEED_A, loc_other_purpose) != v1


def test_reference_and_kernel_agree_over_many_blocks():
    """大样本交叉：200 个区组 ×3 层，全部置换必须逐位一致。"""
    for sk in ["C1|early", "C1|late", "C3|early"]:
        for bi in range(200):
            prod, _ = block_permutation(SEED_A, "TV-STUDY", TV_FP, sk, bi, 4)
            r = ref.ref_permutation(SEED_A, "TV-STUDY", TV_FP, sk, bi, 4)
            assert tuple(prod) == r
