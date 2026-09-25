"""Exhaustive property tests over all tree shapes up to a few hundred leaves.

Every generated inclusion/consistency proof must verify; and a battery of
mutations (wrong index, forged root, flipped hash, truncated/extended
proof, wrong claimed size) must all be rejected.
"""

import os

import pytest

from tl import merkle as M

N_INCLUSION = 300
N_CONSISTENCY = 130


@pytest.fixture(scope="module")
def tree():
    leaves = [M.leaf_hash(os.urandom(8)) for _ in range(N_INCLUSION)]
    roots = [M.tree_hash(leaves[:n]) for n in range(N_INCLUSION + 1)]
    return leaves, roots


def test_every_power_of_two_and_odd_size_root_stable(tree):
    leaves, roots = tree
    # iterative stack algorithm must equal the recursive MTH definition;
    # split k = largest power of two strictly smaller than n.
    for n in (2, 3, 5, 6, 7, 9, 15, 16, 17, 31, 63, 127, 128, 129, 255, 256, 257):
        if n <= N_INCLUSION:
            if n & (n - 1) == 0:
                k = n // 2                       # n=8 -> 4
            else:
                k = 1 << (n.bit_length() - 1)    # n=7 -> 4, n=9 -> 8
            assert roots[n] == M.node_hash(
                M.tree_hash(leaves[:k]), M.tree_hash(leaves[k:n])
            )


def test_inclusion_all_indices_all_sizes(tree):
    leaves, roots = tree
    for n in range(1, N_INCLUSION + 1):
        for idx in range(n):
            proof = M.inclusion_proof_hashes(idx, leaves[:n])
            assert M.verify_inclusion(idx, n, leaves[idx], proof, roots[n])


def test_inclusion_wrong_index_rejected(tree):
    leaves, roots = tree
    for n in (2, 3, 5, 7, 8, 9, 100, 257, 300):
        for idx in range(n):
            proof = M.inclusion_proof_hashes(idx, leaves[:n])
            other = (idx + 1) % n
            assert not M.verify_inclusion(other, n, leaves[idx], proof, roots[n])
            # index out of range
            assert not M.verify_inclusion(n, n, leaves[idx], proof, roots[n])
            assert not M.verify_inclusion(-1, n, leaves[idx], proof, roots[n])


def test_inclusion_forged_root_rejected(tree):
    leaves, roots = tree
    forged = b"\x00" * 32
    for n in (1, 2, 3, 7, 8, 100, 300):
        idx = n // 2
        proof = M.inclusion_proof_hashes(idx, leaves[:n])
        assert not M.verify_inclusion(idx, n, leaves[idx], proof, forged)
        # a root from a different size does not validate
        if n > 1:
            assert not M.verify_inclusion(idx, n, leaves[idx], proof, roots[n - 1])


def test_inclusion_proof_mutations_rejected(tree):
    leaves, roots = tree
    for n in (2, 3, 6, 7, 8, 9, 31, 64, 65, 300):
        idx = n // 2
        proof = list(M.inclusion_proof_hashes(idx, leaves[:n]))
        # flip one bit in the first/last proof element
        for pos in (0, -1):
            bad = [h for h in proof]
            bad[pos] = bytes([bad[pos][0] ^ 0x01]) + bad[pos][1:]
            assert not M.verify_inclusion(idx, n, leaves[idx], bad, roots[n])
        # truncated and extended
        assert not M.verify_inclusion(idx, n, leaves[idx], proof[:-1], roots[n])
        assert not M.verify_inclusion(
            idx, n, leaves[idx], proof + [b"\x00" * 32], roots[n]
        )
        # wrong-length element
        if proof:
            assert not M.verify_inclusion(
                idx, n, leaves[idx], proof[:-1] + [b"x"], roots[n]
            )


def test_consistency_all_pairs(tree):
    leaves, roots = tree
    for n in range(1, N_CONSISTENCY + 1):
        for m in range(1, n + 1):
            proof = M.consistency_proof_hashes(m, leaves[:n])
            assert M.verify_consistency(m, n, roots[m], roots[n], proof), (m, n)


def test_consistency_power_of_two_old_sizes(tree):
    leaves, roots = tree
    n = 129
    for m in (1, 2, 4, 8, 16, 32, 64, 128):
        proof = M.consistency_proof_hashes(m, leaves[:n])
        assert M.verify_consistency(m, n, roots[m], roots[n], proof)


def test_consistency_non_power_of_two_old_sizes(tree):
    leaves, roots = tree
    n = 100
    for m in (3, 5, 6, 7, 9, 15, 17, 31, 33, 63, 65, 99):
        proof = M.consistency_proof_hashes(m, leaves[:n])
        assert M.verify_consistency(m, n, roots[m], roots[n], proof)


def test_consistency_forged_old_root_rejected(tree):
    leaves, roots = tree
    for m, n in [(2, 3), (3, 7), (4, 7), (6, 7), (8, 16), (8, 17), (50, 100)]:
        proof = M.consistency_proof_hashes(m, leaves[:n])
        assert not M.verify_consistency(m, n, b"\x11" * 32, roots[n], proof)


def test_consistency_wrong_old_size_rejected(tree):
    leaves, roots = tree
    for m, n in [(3, 7), (6, 7), (5, 10), (50, 100)]:
        proof = M.consistency_proof_hashes(m, leaves[:n])
        assert not M.verify_consistency(
            m - 1, n, roots[m - 1], roots[n], proof
        )


def test_consistency_tampered_proof_rejected(tree):
    leaves, roots = tree
    for m, n in [(3, 7), (2, 8), (8, 16), (50, 100)]:
        proof = list(M.consistency_proof_hashes(m, leaves[:n]))
        if proof:
            bad = [h for h in proof]
            bad[0] = bytes([bad[0][0] ^ 0xFF]) + bad[0][1:]
            assert not M.verify_consistency(m, n, roots[m], roots[n], bad)
            assert not M.verify_consistency(
                m, n, roots[m], roots[n], proof + [b"\x00" * 32]
            )


def test_consistency_trivial_cases():
    assert M.verify_consistency(0, 0, M.EMPTY_TREE_HASH, M.EMPTY_TREE_HASH, [])
    leaves = [M.leaf_hash(b"a"), M.leaf_hash(b"b")]
    r1, r2 = M.tree_hash(leaves[:1]), M.tree_hash(leaves)
    assert M.verify_consistency(1, 1, r1, r1, [])
    assert not M.verify_consistency(1, 1, r1, r2, [])
    # empty -> anything: vacuously consistent, empty proof only
    assert M.verify_consistency(0, 2, M.EMPTY_TREE_HASH, r2, [])
    assert not M.verify_consistency(0, 2, M.EMPTY_TREE_HASH, r2, [b"x" * 32])
    # malformed inputs never raise
    assert not M.verify_consistency(2, 1, r2, r1, [])
    assert not M.verify_inclusion(0, 0, leaves[0], [], r1)
    assert not M.verify_inclusion(0, 1, b"short", [], r1)
