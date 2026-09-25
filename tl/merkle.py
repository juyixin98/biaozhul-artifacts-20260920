"""Append-only Merkle tree primitives.

Hash scheme follows RFC 9162 (formerly RFC 6962), section 2:

* Empty tree root: SHA256("")
* Leaf hash:       SHA256(0x00 || data)
* Internal hash:   SHA256(0x01 || left_hash || right_hash)

The 0x00 / 0x01 domain-separation prefixes make it infeasible to
present an internal node as a leaf (or vice versa).

Proof generation and verification are direct implementations of the
PATH() (section 2.1.3.1) and SUBPROOF() (section 2.1.3.2) recursions;
the verifiers are exact duals of the generators, so for any 1 <= m <= n
every generated proof verifies, and any hash mutation (wrong index,
forged root, truncated/extended proof) fails.
"""

from __future__ import annotations

import hashlib
from typing import List, Sequence

HASH_SIZE = 32
LEAF_HASH_PREFIX = b"\x00"
NODE_HASH_PREFIX = b"\x01"

#: Fixed root of the empty tree: SHA256("").
EMPTY_TREE_HASH = hashlib.sha256(b"").digest()


def leaf_hash(data: bytes) -> bytes:
    """Hash a single log entry (leaf input) with the leaf domain prefix."""
    return hashlib.sha256(LEAF_HASH_PREFIX + data).digest()


def node_hash(left: bytes, right: bytes) -> bytes:
    """Hash two child hashes with the internal-node domain prefix."""
    return hashlib.sha256(NODE_HASH_PREFIX + left + right).digest()


def _largest_power_of_two_leq(n: int) -> int:
    """Split size k for a (sub)tree of n leaves (RFC 9162 section 2.1.2).

    Largest power of two strictly smaller than n; requires n > 1.
    E.g. n=7 -> 4, n=4 -> 2, n=3 -> 2.
    """
    if n <= 1:
        raise ValueError(f"need n > 1, got {n}")
    if n & (n - 1) == 0:
        return n // 2
    return 1 << (n.bit_length() - 1)


def tree_hash(leaves: Sequence[bytes]) -> bytes:
    """Compute the Merkle tree head for a sequence of *leaf hashes*.

    Uses the iterative right-spine algorithm: fold equal-height perfect
    subtrees together, which is equivalent to the MTH() recursion and is
    O(n) time and O(log n) space.
    """
    stack: List[List] = []  # entries: [subtree_hash, level]
    for h in leaves:
        stack.append([h, 0])
        while len(stack) >= 2 and stack[-1][1] == stack[-2][1]:
            right = stack.pop()
            left = stack.pop()
            stack.append([node_hash(left[0], right[0]), left[1] + 1])
    if not stack:
        return EMPTY_TREE_HASH
    root = stack.pop()
    while stack:
        left = stack.pop()
        # Capture the level before mutating: ``root`` may alias storage that
        # ``left`` referenced inside ``stack`` (entries are mutable lists).
        root = [node_hash(left[0], root[0]), left[1] + 1]
    return root[0]


# ---------------------------------------------------------------------------
# Inclusion proofs (RFC 9162 section 2.1.3.1 / 2.1.4.2)
# ---------------------------------------------------------------------------


def inclusion_proof_hashes(
    leaf_index: int, leaves: Sequence[bytes]
) -> List[bytes]:
    """Inclusion proof for leaf ``leaf_index`` against all ``leaves``.

    Returns the bottom-up list of sibling hashes produced by PATH().
    ``leaves`` are leaf *hashes*; the caller computes them from raw data
    with :func:`leaf_hash`.
    """
    n = len(leaves)
    if not (0 <= leaf_index < n):
        raise IndexError(
            f"leaf_index {leaf_index} out of range for tree_size {n}"
        )
    proof: List[bytes] = []

    def sub(m: int, size: int, start: int) -> None:
        if size <= 1:
            return
        k = _largest_power_of_two_leq(size)
        if m < k:
            sub(m, k, start)
            proof.append(tree_hash(leaves[start + k : start + size]))
        else:
            sub(m - k, size - k, start + k)
            proof.append(tree_hash(leaves[start : start + k]))

    sub(leaf_index, n, 0)
    return proof

class _BadProof(Exception):
    """Internal: malformed proof (bad length, bad element size, truncated)."""


def _as_hash(value: object) -> bytes:
    if not isinstance(value, (bytes, bytearray)) or len(value) != HASH_SIZE:
        raise _BadProof("proof element must be 32 bytes")
    return bytes(value)


def _inclusion_length(m: int, n: int) -> int:
    if n <= 1:
        return 0
    k = _largest_power_of_two_leq(n)
    if m < k:
        return _inclusion_length(m, k) + 1
    return _inclusion_length(m - k, n - k) + 1


def verify_inclusion(
    leaf_index: int,
    tree_size: int,
    leaf: bytes,
    proof: Sequence[bytes],
    root: bytes,
) -> bool:
    """Verify an inclusion proof (dual of the PATH() recursion).

    ``leaf`` is the leaf *hash* of the claimed entry.  Returns True only
    when recomputing from ``leaf`` with ``proof`` yields ``root``.  Any
    malformed input yields False (never raises).
    """
    try:
        if tree_size <= 0 or not (0 <= leaf_index < tree_size):
            return False
        if len(proof) != _inclusion_length(leaf_index, tree_size):
            return False
        _as_hash(leaf)
        _as_hash(root)
        seq = [_as_hash(h) for h in proof]
        it = iter(seq)

        def sub(m: int, size: int, acc: bytes) -> bytes:
            if size <= 1:
                return acc
            k = _largest_power_of_two_leq(size)
            if m < k:
                # leaf in the LEFT half: descend, then join the right sibling
                # (consumed on the way back up, in generator order).
                return node_hash(sub(m, k, acc), next(it))
            # leaf in the RIGHT half: descend, then join the left sibling.
            inner = sub(m - k, size - k, acc)
            return node_hash(next(it), inner)

        return sub(leaf_index, tree_size, leaf) == root
    except (_BadProof, StopIteration, RecursionError):
        return False


# ---------------------------------------------------------------------------
# Consistency proofs (RFC 9162 section 2.1.3.2 / 2.1.4.4)
# ---------------------------------------------------------------------------


def consistency_proof_hashes(
    old_size: int, new_leaves: Sequence[bytes]
) -> List[bytes]:
    """Consistency proof from ``old_size`` leaves to len(new_leaves) leaves.

    Wire format (PROOF() of RFC 9162): when old_size is a power of two,
    the old root is NOT included (the verifier prepends the root it
    already holds); otherwise the first element is the SUBPROOF seed.
    """
    n = len(new_leaves)
    if not (0 <= old_size <= n):
        raise ValueError(
            f"require 0 <= old_size ({old_size}) <= new_size ({n})"
        )
    if old_size == 0 or old_size == n:
        return []

    proof: List[bytes] = []

    def sub(m: int, size: int, start: int, old_path: bool) -> None:
        if m == size:
            # Terminal on the b=true ("old head") spine contributes nothing;
            # the verifier already holds that root.
            if not old_path:
                proof.append(tree_hash(new_leaves[start : start + size]))
            return
        k = _largest_power_of_two_leq(size)
        if m <= k:
            sub(m, k, start, old_path)
            proof.append(tree_hash(new_leaves[start + k : start + size]))
        else:
            sub(m - k, size - k, start + k, False)
            proof.append(tree_hash(new_leaves[start : start + k]))

    sub(old_size, n, 0, True)
    return proof


def verify_consistency(
    old_size: int,
    new_size: int,
    old_root: bytes,
    new_root: bytes,
    proof: Sequence[bytes],
) -> bool:
    """Verify an append-only consistency proof (RFC 9162 section 2.1.4.4).

    True iff the first ``old_size`` leaves of the new tree hash to
    ``old_root`` and the whole ``new_size`` tree hashes to ``new_root``.
    Malformed inputs yield False.
    """
    try:
        if new_size < old_size or old_size < 0 or new_size < 0:
            return False
        old_root = _as_hash(old_root)
        new_root = _as_hash(new_root)
        seq = [_as_hash(h) for h in proof]
    except _BadProof:
        return False

    if old_size == 0:
        # The empty hash is fixed and universally known: no proof needed.
        if len(seq) != 0:
            return False
        return new_root == EMPTY_TREE_HASH if new_size == 0 else True
    if old_size == new_size:
        return len(seq) == 0 and old_root == new_root

    pow2 = old_size & (old_size - 1) == 0
    # Exact expected proof length = number of hashes the generator's
    # SUBPROOF emits; reject truncated/extended proofs before hashing.
    def emitted(m: int, size: int, old_path: bool) -> int:
        if m == size:
            return 0 if old_path else 1
        k = _largest_power_of_two_leq(size)
        if m <= k:
            return emitted(m, k, old_path) + 1
        return emitted(m - k, size - k, False) + 1

    if len(seq) != emitted(old_size, new_size, True):
        return False

    # Only the generator's emitted hashes are consumed; for a power-of-two
    # old size the verifier seeds the accumulators with the old root it
    # already holds (RFC 9162 2.1.4.4) instead of reading it from the proof.
    it: "iter" = iter(seq)
    try:
        def sub(m: int, size: int, old_path: bool):
            """Dual of the generator: return (old-edge FR, new-edge SR).

            FR is None when the seed subtree is the (power-of-two) old tree;
            the verifier then checks the root it already holds.
            """
            if m == size:
                if old_path:
                    # Only reached for the power-of-two seed: the whole
                    # subtree is the old tree, whose root the verifier holds.
                    assert pow2
                    return None, old_root
                h = next(it)
                return h, h
            k = _largest_power_of_two_leq(size)
            if m <= k:
                fr, sr = sub(m, k, old_path)
                c = next(it)  # right sibling at this level
                return fr, node_hash(sr, c)
            fr, sr = sub(m - k, size - k, False)
            c = next(it)  # left sibling at this level
            assert fr is not None
            return node_hash(c, fr), node_hash(c, sr)

        fr, sr = sub(old_size, new_size, True)
        if next(it, None) is not None:
            return False
        return sr == new_root and (pow2 or fr == old_root)
    except (_BadProof, StopIteration, AssertionError, RecursionError):
        return False
