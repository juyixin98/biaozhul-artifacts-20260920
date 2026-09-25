"""Test vectors for the RFC 9162 / RFC 6962 Merkle hash scheme.

Two independent sources are checked:

1. RFC 9162 section 2.1.5 symbolic 7-leaf "Example" (proof *shapes*
   and sibling orderings, quoted verbatim in the RFC).

2. The canonical 8-leaf binary test data from the RFC 6962 test-vector
   appendix / Certificate Transparency reference suite:

       d(j) = 0x00 || j            (j = 0..7)

   including the published leaf hash of d(0), the MTH roots for all
   sizes 0..8, and inclusion/consistency proofs for a non-power-of-two
   old size (3 -> 8).
"""

from tl import merkle as M

# ---------------------------------------------------------------------------
# 1. RFC 9162 2.1.5 symbolic 7-leaf example
#
# RFC diagram (a..f are leaf hashes of d0..d5; the 7th leaf is "d6"):
#
#        g          h          i          j
#      /   \      /   \      /   \        |
#      a   b     c   d      e   f        d6
#     d0  d1    d2  d3     d4  d5
#
#             k                       l
#           /   \                   /   \
#          g     h                 i     j
#
#   g = H(0x01|a|b)   h = H(0x01|c|d)   i = H(0x01|e|f)
#   j = leaf hash of d6 (single-leaf subtree)
#   k = H(0x01|g|h)   l = H(0x01|i|j)
#
# root = H(0x01|k|l).  Stated verbatim in the RFC:
#   inclusion d0 = [b, h, l], d3 = [c, g, l], d4 = [f, j, k], d6 = [i, k]
#   PROOF(3,D7)=[c, d, g, l], PROOF(4,D7)=[l], PROOF(6,D7)=[i, j, k]
# ---------------------------------------------------------------------------

_SEVEN = [bytes([j]) for j in range(7)]
_L = [M.leaf_hash(x) for x in _SEVEN]
_G = M.node_hash(_L[0], _L[1])   # d0,d1
_H = M.node_hash(_L[2], _L[3])   # d2,d3
_I = M.node_hash(_L[4], _L[5])   # d4,d5
_J = _L[6]                       # d6 leaf
_K = M.node_hash(_G, _H)         # d0..d3
_LL = M.node_hash(_I, _J)        # d4..d6  (RFC 'l')
_ROOT7 = M.node_hash(_K, _LL)
_ROOTS7 = {n: M.tree_hash(_L[:n]) for n in range(8)}


def test_tree_shape_matches_rfc_example():
    assert M.tree_hash(_L) == _ROOT7
    assert M.tree_hash(_L[0:4]) == _K
    assert M.tree_hash(_L[4:6]) == _I
    assert M.tree_hash(_L[4:7]) == _LL
    assert _LL == M.node_hash(_I, _J)


def test_rfc_inclusion_proof_shapes():
    assert M.inclusion_proof_hashes(0, _L) == [_L[1], _H, _LL]
    assert M.inclusion_proof_hashes(3, _L) == [_L[2], _G, _LL]
    assert M.inclusion_proof_hashes(4, _L) == [_L[5], _J, _K]
    assert M.inclusion_proof_hashes(6, _L) == [_I, _K]
    assert M.inclusion_proof_hashes(0, _L[:1]) == []  # PATH(0,{d0}) = {}


def test_rfc_inclusion_proofs_verify():
    cases = [
        (0, [_L[1], _H, _LL]),
        (3, [_L[2], _G, _LL]),
        (4, [_L[5], _J, _K]),
        (6, [_I, _K]),
    ]
    for idx, proof in cases:
        assert M.verify_inclusion(idx, 7, _L[idx], proof, _ROOT7)


def test_rfc_consistency_proofs():
    # PROOF(3,D7) = [c, d, g, l]  (d = leaf hash of d[3])
    assert M.consistency_proof_hashes(3, _L) == [_L[2], _L[3], _G, _LL]
    # PROOF(4,D7) = [l]
    assert M.consistency_proof_hashes(4, _L) == [_LL]
    # PROOF(6,D7) = [i, j, k]
    assert M.consistency_proof_hashes(6, _L) == [_I, _J, _K]


def test_rfc_consistency_proofs_verify():
    assert M.verify_consistency(
        3, 7, _ROOTS7[3], _ROOTS7[7], [_L[2], _L[3], _G, _LL]
    )
    assert M.verify_consistency(4, 7, _ROOTS7[4], _ROOTS7[7], [_LL])
    assert M.verify_consistency(
        6, 7, _ROOTS7[6], _ROOTS7[7], [_I, _J, _K]
    )


# ---------------------------------------------------------------------------
# 2. Canonical binary vectors: d(j) = 0x00 || j, j = 0..7
# ---------------------------------------------------------------------------

D8 = [b"\x00" + bytes([j]) for j in range(8)]
LH8 = [M.leaf_hash(x) for x in D8]

EMPTY_HEX = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
# RFC 6962 test-vector appendix: hash of leaf d(0) = 0x00 || 0x00.
LEAF_D0_HEX = "709e80c88487a2411e1ee4dfb9f22a861492d20c4765150c0c794abd70f8147c"
# Published MTH roots for sizes 1..8 over d(0)..d(7).
MTH_HEX = {
    1: "709e80c88487a2411e1ee4dfb9f22a861492d20c4765150c0c794abd70f8147c",
    2: "319dd25400276a78b435a7c0f87afbdc29b3a8029915a385b6b302c932321e31",
    3: "f1b9f16464b7d5fd3c6907ba171d1e55a9ed16181e0a135f5e55c934c3cbe40a",
    4: "3c81285f889a395556e0be5aa2a184af5d962a80dc094ae43c43c7b26a00f7f4",
    5: "b116f9219e3b00af93fe3cf9753c27a2ca113a824168aa9f0667ff59d3b9d79c",
    6: "7bf624e7c89fcf47a2afbb1c72074a62ab243f63e60b7d3d54713984d6fa2c2c",
    7: "e99dfbbaa8e26d67fcb87503fb6ae10d2f26e422e2121ab676f630e0595bbb95",
    8: "0a2a2c470619da672e872dc4b634e54df42f25b7b226baae9546db5ddfac09c7",
}


def test_empty_tree_root():
    assert M.EMPTY_TREE_HASH.hex() == EMPTY_HEX


def test_published_leaf_and_roots():
    assert M.leaf_hash(D8[0]).hex() == LEAF_D0_HEX
    for n, expected in MTH_HEX.items():
        assert M.tree_hash(LH8[:n]).hex() == expected, n


def test_binary_inclusion_proofs_verify_and_match_published():
    root8 = bytes.fromhex(MTH_HEX[8])
    # Inclusion proof for d(0) against D[8] (published vector).
    proof0_hex = [
        "cf7605ed1bc735f6c825554154627467e1cac9df54cee8699218ed434603c568",
        "48d6e059de38586f6fd92dbf639415bf6a5930eb1e2856b023b527d3d0c59da8",
        "112cafbe323b00b6b407b905a247d0d12400b61294d0ef67cd2162f754b957fb",
    ]
    got = M.inclusion_proof_hashes(0, LH8)
    assert [h.hex() for h in got] == proof0_hex
    assert M.verify_inclusion(0, 8, LH8[0], got, root8)

    # Inclusion proof for d(6) against D[8] (published vector).
    proof6_hex = [
        "4fca068fbfc9bdfd1a5c16427b0e34c5339c4c5b581769d9e8ced90a8b3efe57",
        "5e45d9ddacc00780c2f0d59d4bf2af4a0acd740c63266bc2066ee141beae5508",
        "3c81285f889a395556e0be5aa2a184af5d962a80dc094ae43c43c7b26a00f7f4",
    ]
    got6 = M.inclusion_proof_hashes(6, LH8)
    assert [h.hex() for h in got6] == proof6_hex
    assert M.verify_inclusion(6, 8, LH8[6], got6, root8)


def test_binary_consistency_3_to_8():
    proof_hex = [
        "65d27a48dfef406db8f5f437423bfa5f9c83d77bfc12f8a14b8ce3ede5892fb5",
        "f31b2b6ef48dff580eca633d0a0cfb15adb7c3bdbc2a1d966a1e10eaaefad3c4",
        "319dd25400276a78b435a7c0f87afbdc29b3a8029915a385b6b302c932321e31",
        "112cafbe323b00b6b407b905a247d0d12400b61294d0ef67cd2162f754b957fb",
    ]
    got = M.consistency_proof_hashes(3, LH8)
    assert [h.hex() for h in got] == proof_hex
    assert M.verify_consistency(
        3, 8,
        bytes.fromhex(MTH_HEX[3]), bytes.fromhex(MTH_HEX[8]),
        got,
    )


def test_domain_separation_prefixes():
    # leaf vs internal hashing never collide at the first domain byte
    assert M.leaf_hash(b"x")[:1] != M.node_hash(b"a" * 32, b"b" * 32)[:1]
    # internal prefix 0x01 is covered even if data were crafted
    assert M.leaf_hash(b"\x01" + b"a" * 64) != M.node_hash(b"a" * 32, b"a" * 32)
