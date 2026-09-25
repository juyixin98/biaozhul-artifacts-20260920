"""Merkle 树核心测试（本项目最重要的一组测试）。

验证策略：使用**三套互相独立的实现**交叉比对，避免「同一个错误写两遍」：

1. 生产代码 ``tlog.merkle``：逐行翻译 RFC 9162 伪代码（递归生成 + LSB 循环验证）。
2. 本文件内的「朴素自底向上」参考实现：逐级相邻配对、奇数节点上提，
   既独立算根，也独立按坐标行走生成包含路径。
3. 手工锚点 + RFC 6962 §2.1.3 七叶树的**符号结构向量**
   （d0→[b,h,l]、d3→[c,g,l]、d4→[f,j,k]、d6→[i,k]；
   PROOF(3,7)=[c,d,g,l]、PROOF(4,7)=[l]、PROOF(6,7)=[i,j,k]）。

覆盖重点（验收要求）：
* 小树逐项核验：n=1..33 的每一片叶子逐条验证，另含随机大一些的树。
* 非二次幂叶数：3,5,6,7,9,15,17,31,33,255,257,1000 等。
* 伪造旧根、错误索引、路径截断/追加/元素替换等全部必须拒绝。
"""

from __future__ import annotations

import hashlib
import os
import random
import unittest

from tlog.hashing import EMPTY_TREE_HASH, HASH_SIZE, leaf_hash, node_hash
from tlog.merkle import (
    consistency_proof,
    inclusion_proof,
    tree_root,
    verify_consistency,
    verify_inclusion,
)


# ---------------------------------------------------------------------------
# 独立参考实现 1：自底向上相邻配对（奇数节点直接上提）
# ---------------------------------------------------------------------------


def reference_root(leaves: list[bytes]) -> bytes:
    level = [leaf_hash(d) for d in leaves]
    if not level:
        return hashlib.sha256(b"").digest()
    while len(level) > 1:
        nxt = []
        i = 0
        while i + 1 < len(level):
            nxt.append(node_hash(level[i], level[i + 1]))
            i += 2
        if i < len(level):
            nxt.append(level[i])  # 奇数个节点：最后一个原样上提
        level = nxt
    return level[0]


def reference_inclusion_path(leaves: list[bytes], target: int) -> list[bytes]:
    """按区间坐标独立行走，记录目标节点每一层的兄弟哈希。"""
    level = [leaf_hash(d) for d in leaves]
    lo = list(range(len(leaves)))
    hi = [i + 1 for i in range(len(leaves))]
    path: list[bytes] = []
    while len(level) > 1:
        nxt: list[bytes] = []
        nlo: list[int] = []
        nhi: list[int] = []
        i = 0
        while i + 1 < len(level):
            if lo[i] <= target < hi[i + 1]:
                if target < hi[i]:
                    path.append(level[i + 1])  # 目标在左，兄弟在右
                else:
                    path.append(level[i])  # 目标在右，兄弟在左
            nxt.append(node_hash(level[i], level[i + 1]))
            nlo.append(lo[i])
            nhi.append(hi[i + 1])
            i += 2
        if i < len(level):  # 奇数尾节点上提，不配对、无兄弟
            nxt.append(level[i])
            nlo.append(lo[i])
            nhi.append(hi[i])
        level, lo, hi = nxt, nlo, nhi
    return path


# ---------------------------------------------------------------------------
# 手工锚点
# ---------------------------------------------------------------------------


def sha256(b: bytes) -> bytes:
    return hashlib.sha256(b).digest()


class AnchorVectorTests(unittest.TestCase):
    """可独立手算/与 RFC 示例对照的硬编码锚点。"""

    def test_empty_tree_hash_is_sha256_of_empty_input(self):
        # MTH({}) = HASH()；SHA256("") 的知名值。
        self.assertEqual(
            EMPTY_TREE_HASH.hex(),
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )
        self.assertEqual(tree_root([]), EMPTY_TREE_HASH)

    def test_single_empty_leaf(self):
        # MTH({""}) = SHA256(0x00)，与 RFC 6962 生态广泛使用的向量一致。
        self.assertEqual(
            leaf_hash(b"").hex(),
            "6e340b9cffb37a989ca544e6bb780a2c78901d3fb33738768511a30617afa01d",
        )
        self.assertEqual(tree_root([b""]), leaf_hash(b""))

    def test_two_empty_leaves_manual(self):
        # MTH({"",""}) = SHA256(0x01 || SHA256(0x00) || SHA256(0x00))
        manual = sha256(b"\x01" + leaf_hash(b"") + leaf_hash(b""))
        self.assertEqual(tree_root([b"", b""]), manual)

    def test_domain_separation_prefixes(self):
        # 0x00/0x01 域分离：叶子与内部节点命名空间不同。
        self.assertNotEqual(sha256(b"\x00" + b"x" + b"y"), sha256(b"\x01xy"))
        # 单叶根不可能等于任一内部节点（不同前缀，第二原像防护的基础）。
        self.assertNotEqual(tree_root([b"ab"]), tree_root([b"a", b"b"]))
        self.assertNotEqual(tree_root([b"ab"]), node_hash(leaf_hash(b"a"), leaf_hash(b"b")))


# ---------------------------------------------------------------------------
# 交叉验证：逐叶、逐树大小
# ---------------------------------------------------------------------------


class CrossCheckTests(unittest.TestCase):
    def test_root_matches_reference_for_small_trees(self):
        for n in range(0, 34):
            leaves = [f"leaf-{n}-{i}".encode() for i in range(n)]
            self.assertEqual(
                tree_root(leaves),
                reference_root(leaves),
                msg=f"树根不一致，n={n}",
            )

    def test_root_matches_reference_for_non_power_of_two_and_large_sizes(self):
        sizes = [2, 3, 5, 6, 7, 9, 15, 17, 31, 33, 63, 64, 65, 127, 128, 129,
                 255, 256, 257, 511, 513, 1000, 1023, 1024, 1025]
        for n in sizes:
            leaves = [os.urandom(7) for _ in range(n)]
            self.assertEqual(
                tree_root(leaves),
                reference_root(leaves),
                msg=f"树根不一致，n={n}",
            )

    def test_every_leaf_small_trees_item_by_item(self):
        """验收：小树（n=1..33）的每一片叶子逐项核验包含证明。"""
        checked = 0
        for n in range(1, 34):
            leaves = [f"记录#{n}-{i}".encode() for i in range(n)]
            root = tree_root(leaves)
            for m in range(n):
                proof = inclusion_proof(m, leaves)
                # (a) 与独立坐标行走实现逐元素一致
                self.assertEqual(
                    proof,
                    reference_inclusion_path(leaves, m),
                    msg=f"路径与参考实现不一致：n={n}, m={m}",
                )
                # (b) RFC 验证器接受
                self.assertTrue(
                    verify_inclusion(leaf_hash(leaves[m]), m, n, proof, root),
                    msg=f"合法证明被拒绝：n={n}, m={m}",
                )
                # (c) 路径长度上界 ceil(log2(n))（n=1 时为 0）
                self.assertLessEqual(len(proof), (n - 1).bit_length())
                checked += 1
        self.assertGreater(checked, 500)  # 至少核验 1..33 共 561 片叶子

    def test_every_leaf_random_trees(self):
        rng = random.Random(20260924)
        for n in [37, 50, 100, 200, 257, 333]:
            leaves = [rng.randbytes(11) for _ in range(n)]
            root = tree_root(leaves)
            for m in range(n):
                proof = inclusion_proof(m, leaves)
                self.assertEqual(proof, reference_inclusion_path(leaves, m))
                self.assertTrue(
                    verify_inclusion(leaf_hash(leaves[m]), m, n, proof, root)
                )

    def test_single_leaf_tree_empty_proof_verifies(self):
        # PATH(0, {d0}) = {}
        root = leaf_hash(b"only")
        self.assertEqual(inclusion_proof(0, [b"only"]), [])
        self.assertTrue(verify_inclusion(leaf_hash(b"only"), 0, 1, [], root))

    def test_tree_root_changes_with_each_append(self):
        # 只增语义：每个前缀根都不同（极大概率），且与参考实现一致。
        leaves = [f"v{i}".encode() for i in range(20)]
        roots = {tree_root(leaves[:n]) for n in range(21)}
        self.assertEqual(len(roots), 21)


# ---------------------------------------------------------------------------
# RFC 6962 §2.1.3 七叶树符号结构向量（非二次幂 7）
# ---------------------------------------------------------------------------


class Rfc6962SevenLeafTreeTests(unittest.TestCase):
    def setUp(self):
        # 用可区分的叶子内容，使每个子树哈希可精确指代。
        self.d = [f"d{i}".encode() for i in range(7)]

    def _root(self, start: int, end: int) -> bytes:
        return tree_root(self.d[start:end])

    def test_inclusion_paths_match_symbolic_vectors(self):
        # 标签（由 RFC 示例反解的精确身份）：
        b = leaf_hash(self.d[1])
        c = leaf_hash(self.d[2])
        d3 = leaf_hash(self.d[3])
        f = leaf_hash(self.d[5])
        g = self._root(0, 2)   # MTH(d0,d1)
        h = self._root(2, 4)   # MTH(d2,d3)
        i = self._root(4, 6)   # MTH(d4,d5)
        j = leaf_hash(self.d[6])
        k = self._root(0, 4)   # MTH(d0..d3)
        l = self._root(4, 7)   # MTH(d4..d6)

        self.assertEqual(inclusion_proof(0, self.d), [b, h, l])
        self.assertEqual(inclusion_proof(3, self.d), [c, g, l])
        self.assertEqual(inclusion_proof(4, self.d), [f, j, k])
        self.assertEqual(inclusion_proof(6, self.d), [i, k])

        root = tree_root(self.d)
        for m in range(7):
            self.assertTrue(
                verify_inclusion(
                    leaf_hash(self.d[m]),
                    m,
                    7,
                    inclusion_proof(m, self.d),
                    root,
                )
            )

    def test_consistency_proofs_match_symbolic_vectors(self):
        c = leaf_hash(self.d[2])
        d3 = leaf_hash(self.d[3])
        g = self._root(0, 2)
        i = self._root(4, 6)
        j = leaf_hash(self.d[6])
        k = self._root(0, 4)
        l = self._root(4, 7)

        # PROOF(3, D[7]) = [c, d, g, l]
        self.assertEqual(consistency_proof(3, self.d), [c, d3, g, l])
        # PROOF(4, D[7]) = [l]（旧大小恰为二次幂，验证时会前置旧根）
        self.assertEqual(consistency_proof(4, self.d), [l])
        # PROOF(6, D[7]) = [i, j, k]
        self.assertEqual(consistency_proof(6, self.d), [i, j, k])

        root7 = tree_root(self.d)
        for first in (3, 4, 6):
            proof = consistency_proof(first, self.d)
            self.assertTrue(
                verify_consistency(
                    first, 7, proof, tree_root(self.d[:first]), root7
                )
            )


# ---------------------------------------------------------------------------
# 一致性证明：全组合 + 边界
# ---------------------------------------------------------------------------


class ConsistencyTests(unittest.TestCase):
    def test_all_pairs_small_trees(self):
        for n in range(2, 40):
            leaves = [f"c-{n}-{i}".encode() for i in range(n)]
            new_root = tree_root(leaves)
            for first in range(1, n):
                proof = consistency_proof(first, leaves)
                old_root = tree_root(leaves[:first])
                self.assertTrue(
                    verify_consistency(first, n, proof, old_root, new_root),
                    msg=f"合法一致性证明被拒绝：first={first}, n={n}",
                )

    def test_all_pairs_larger(self):
        rng = random.Random(77)
        for n in [64, 65, 100, 127, 128, 129, 257, 500]:
            leaves = [rng.randbytes(9) for _ in range(n)]
            new_root = tree_root(leaves)
            for first in (1, 2, 3, 4, n // 2, n - 3, n - 2, n - 1):
                proof = consistency_proof(first, leaves)
                self.assertTrue(
                    verify_consistency(
                        first, n, proof, tree_root(leaves[:first]), new_root
                    ),
                    msg=f"first={first}, n={n}",
                )

    def test_power_of_two_old_size_prepends_old_root_in_verifier(self):
        # first=4（二次幂）：证明只有右侧节点，验证器内部会前置旧根。
        leaves = [str(i).encode() for i in range(7)]
        proof = consistency_proof(4, leaves)
        self.assertEqual(proof, [tree_root(leaves[4:])])
        self.assertTrue(
            verify_consistency(
                4, 7, proof, tree_root(leaves[:4]), tree_root(leaves)
            )
        )

    def test_same_size_is_consistent_only_when_roots_equal(self):
        leaves = [b"a", b"b", b"c"]
        root = tree_root(leaves)
        self.assertTrue(verify_consistency(3, 3, [], root, root))
        self.assertFalse(verify_consistency(3, 3, [], root, b"\x11" * 32))
        self.assertFalse(verify_consistency(3, 3, [b"\x22" * 32], root, root))

    def test_consistency_detects_divergent_prefix(self):
        """关键安全属性：旧前缀不同的两棵树，证明不可能通过。"""
        leaves_a = [f"a{i}".encode() for i in range(7)]
        leaves_b = [f"b{i}".encode() for i in range(5)] + [
            f"a{i}".encode() for i in range(5, 7)
        ]
        self.assertEqual(len(leaves_b), 7)
        proof = consistency_proof(5, leaves_a)  # A 日志给出的真实证明
        root_a5 = tree_root(leaves_a[:5])
        root_b7 = tree_root(leaves_b)
        # 拿 A 的证明去关联 B 的（伪造）旧根/新根，必须失败。
        self.assertFalse(
            verify_consistency(5, 7, proof, tree_root(leaves_b[:5]), root_b7)
        )
        self.assertFalse(
            verify_consistency(5, 7, proof, root_a5, root_b7)
        )

    def test_invalid_consistency_inputs_rejected(self):
        leaves = [b"x"] * 6
        root = tree_root(leaves)
        proof = consistency_proof(3, leaves)
        bad = b"\x00" * 32
        self.assertFalse(verify_consistency(0, 6, proof, root, root))
        self.assertFalse(verify_consistency(3, 6, [], root, root))
        self.assertFalse(verify_consistency(7, 6, proof, root, root))
        self.assertFalse(verify_consistency(-1, 6, proof, root, root))
        self.assertFalse(verify_consistency(3, 6, proof, bad, root))   # 伪旧根
        self.assertFalse(verify_consistency(3, 6, proof, root, bad))  # 伪新根
        self.assertFalse(
            verify_consistency(3, 6, proof + [bad], root, root)
        )  # 路径追加
        self.assertFalse(
            verify_consistency(3, 6, proof[:-1], root, root)
        )  # 路径截断

    def test_generator_rejects_bad_sizes(self):
        leaves = [b"x" * 2 for _ in range(5)]
        with self.assertRaises(ValueError):
            consistency_proof(0, leaves)
        with self.assertRaises(ValueError):
            consistency_proof(5, leaves)
        with self.assertRaises(ValueError):
            consistency_proof(6, leaves)


# ---------------------------------------------------------------------------
# 包含证明的攻击 / 畸形输入（验收：伪造旧根与错误索引等）
# ---------------------------------------------------------------------------


class InclusionAttackTests(unittest.TestCase):
    def setUp(self):
        self.leaves = [f"条目-{i}".encode() for i in range(11)]
        self.root = tree_root(self.leaves)

    def test_forged_root_rejected(self):
        proof = inclusion_proof(3, self.leaves)
        forged = bytes(self.root[i] ^ 0x01 for i in range(HASH_SIZE))
        self.assertNotEqual(forged, self.root)
        self.assertFalse(
            verify_inclusion(leaf_hash(self.leaves[3]), 3, 11, proof, forged)
        )

    def test_wrong_index_rejected(self):
        proof = inclusion_proof(3, self.leaves)
        for wrong in (0, 1, 2, 4, 5, 10):
            self.assertFalse(
                verify_inclusion(
                    leaf_hash(self.leaves[3]), wrong, 11, proof, self.root
                ),
                msg=f"错误索引 {wrong} 竟然通过",
            )
        # 越界索引
        self.assertFalse(
            verify_inclusion(leaf_hash(self.leaves[3]), 11, 11, proof, self.root)
        )
        self.assertFalse(
            verify_inclusion(leaf_hash(self.leaves[3]), -1, 11, proof, self.root)
        )

    def test_other_leaf_hash_rejected(self):
        # 用 m=3 的路径，但声称叶子是另一项
        proof = inclusion_proof(3, self.leaves)
        for other in (0, 2, 4, 10):
            self.assertFalse(
                verify_inclusion(
                    leaf_hash(self.leaves[other]), 3, 11, proof, self.root
                )
            )

    def test_tampered_path_elements_rejected(self):
        proof = inclusion_proof(5, self.leaves)
        for pos in range(len(proof)):
            tampered = list(proof)
            tampered[pos] = bytes(tampered[pos][i] ^ 0xFF for i in range(HASH_SIZE))
            self.assertFalse(
                verify_inclusion(
                    leaf_hash(self.leaves[5]), 5, 11, tampered, self.root
                )
            )

    def test_truncated_and_appended_paths_rejected(self):
        proof = inclusion_proof(2, self.leaves)
        for cut in range(1, len(proof) + 1):
            self.assertFalse(
                verify_inclusion(
                    leaf_hash(self.leaves[2]), 2, 11, proof[:-cut], self.root
                )
            )
        self.assertFalse(
            verify_inclusion(
                leaf_hash(self.leaves[2]),
                2,
                11,
                proof + [b"\xaa" * HASH_SIZE],
                self.root,
            )
        )
        # 路径顺序打乱也必须失败
        reordered = list(reversed(proof))
        self.assertFalse(
            verify_inclusion(
                leaf_hash(self.leaves[2]), 2, 11, reordered, self.root
            )
        )

    def test_wrong_tree_size_rejected(self):
        # 针对大小 11 生成的证明（路径含 4 个兄弟，树高为 4）。
        # 声称一个高度不足（<=8）或需要更多路径层（>=17）的树大小，
        # 必然在 LSB 循环中途 sn 归零或末尾 sn!=0，必须拒绝。
        proof = inclusion_proof(3, self.leaves)
        root11 = self.root
        for fake_size in (1, 2, 7, 8, 17, 20, 32):
            self.assertFalse(
                verify_inclusion(
                    leaf_hash(self.leaves[3]), 3, fake_size, proof, root11
                ),
                msg=f"伪造树大小 {fake_size} 竟然通过",
            )
        # 针对旧树（大小 7）生成的真证明，配当前根必须失败。
        old_proof = inclusion_proof(3, self.leaves[:7])
        self.assertTrue(
            verify_inclusion(
                leaf_hash(self.leaves[3]), 3, 7, old_proof, tree_root(self.leaves[:7])
            )
        )
        self.assertFalse(
            verify_inclusion(
                leaf_hash(self.leaves[3]), 3, 7, old_proof, root11
            )
        )

    def test_tree_size_must_come_from_signed_sth_not_trusted_with_proof(self):
        """记录 RFC 9162 验证语义（非缺陷）：验证器不校验路径长度。

        实测：m=3、真实 n=11 的证明（4 个路径元素，树高 4），谎报为
        9..16 中任一树大小时，(fn, sn) 的最低位走法都能以相同顺序消费
        完路径，并重建出**真实的 11 叶根**。这正是协议要求 tree_size
        与 root_hash 必须来自**已验签的 STH**、不能随证明一起信任的
        原因：正确用法里验证者钉住 (tree_size=11, root=root11)，
        日志无法让它去试别的大小；而若日志真给出 16 叶的 STH，其根是
        另一个值（且 STH 有签名），该证明对那个根验不过。
        """
        proof = inclusion_proof(3, self.leaves)
        for claimed_size in range(9, 17):  # 9..16 全部重建出真实 11 叶根
            self.assertTrue(
                verify_inclusion(
                    leaf_hash(self.leaves[3]),
                    3,
                    claimed_size,
                    proof,
                    self.root,  # 始终钉住 11 叶根（来自已验签 STH）
                ),
                msg="同一位形下重建出的根应为真实 11 叶根",
            )
        # 同样的路径对 16 叶的真实树根则不可能通过。
        root16 = tree_root(self.leaves + [b"more"] * 5)
        self.assertFalse(
            verify_inclusion(
                leaf_hash(self.leaves[3]), 3, 16, proof, root16
            )
        )

    def test_malformed_hash_lengths_rejected(self):
        proof = inclusion_proof(3, self.leaves)
        self.assertFalse(
            verify_inclusion(b"short", 3, 11, proof, self.root)
        )
        self.assertFalse(
            verify_inclusion(
                leaf_hash(self.leaves[3]), 3, 11, proof, b"short-root"
            )
        )
        self.assertFalse(
            verify_inclusion(
                leaf_hash(self.leaves[3]),
                3,
                11,
                proof + [b"x"],
                self.root,
            )
        )

    def test_zero_tree_size_rejected(self):
        self.assertFalse(
            verify_inclusion(
                leaf_hash(b"whatever"), 0, 0, [], EMPTY_TREE_HASH
            )
        )

    def test_generator_bounds(self):
        leaves = [b"a", b"b", b"c"]
        with self.assertRaises(IndexError):
            inclusion_proof(3, leaves)
        with self.assertRaises(IndexError):
            inclusion_proof(-1, leaves)


if __name__ == "__main__":
    unittest.main(verbosity=2)
