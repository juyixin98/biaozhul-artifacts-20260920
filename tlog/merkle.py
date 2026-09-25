"""只增 Merkle 树：树头（根）计算、包含证明、一致性证明。

算法逐条对应 RFC 9162 §2.1 的伪代码：

* :func:`tree_root`                —— MTH(D[n])
* :func:`inclusion_proof`          —— PATH(m, D[n])
* :func:`verify_inclusion`         —— RFC 9162 §2.1.6.2 的 LSB 循环验证
* :func:`consistency_proof`        —— PROOF(m, D[n]) / SUBPROOF
* :func:`verify_consistency`       —— RFC 9162 §2.1.4.2 的双运行哈希验证

约定：输入是「叶子数据」列表（原始字节）。叶子哈希在 :mod:`tlog.hashing`
中用 0x00 前缀计算，内部节点用 0x01 前缀，二者域分离。
"""

from __future__ import annotations

from .hashing import EMPTY_TREE_HASH, HASH_SIZE, leaf_hash, node_hash


def _largest_pow2_less_than(n: int) -> int:
    """返回严格小于 n 的最大二次幂 k（满足 k < n <= 2k），仅在 n > 1 时调用。

    注意必须是「严格小于」：n 本身是二次幂时（如 n=4）应返回 n/2=2，
    用 (n-1).bit_length() - 1 而不是 n.bit_length() - 1。
    """
    if n <= 1:
        raise ValueError(f"n 必须大于 1，实际为 {n}")
    return 1 << ((n - 1).bit_length() - 1)


def tree_root(leaves: list[bytes]) -> bytes:
    """计算叶子列表的 Merkle 树根 MTH(D[n])。

    自底向上 + 区间记忆化，O(n) 时间 / O(n) 空间。
    空树返回 :data:`tlog.hashing.EMPTY_TREE_HASH`。
    """
    n = len(leaves)
    if n == 0:
        return EMPTY_TREE_HASH

    # memo[(start, end)] = MTH(leaves[start:end])
    memo: dict[tuple[int, int], bytes] = {}

    def mth(start: int, end: int) -> bytes:
        key = (start, end)
        cached = memo.get(key)
        if cached is not None:
            return cached
        count = end - start
        if count == 1:
            result = leaf_hash(leaves[start])
        else:
            k = _largest_pow2_less_than(count)
            result = node_hash(mth(start, start + k), mth(start + k, end))
        memo[key] = result
        return result

    return mth(0, n)


def inclusion_proof(leaf_index: int, leaves: list[bytes]) -> list[bytes]:
    """生成包含证明 PATH(leaf_index, D[n])，即从叶子到树根路径上的兄弟哈希列表。

    要求 0 <= leaf_index < n。单叶树返回空列表（RFC：PATH(0, {d0}) = {}）。
    严格按 RFC 9162 §2.1.5 的递归定义实现。
    """
    n = len(leaves)
    if not (0 <= leaf_index < n):
        raise IndexError(
            f"叶子索引 {leaf_index} 越界：树大小为 {n}"
        )
    if n == 1:
        return []

    root_memo: dict[tuple[int, int], bytes] = {}

    def mth(start: int, end: int) -> bytes:
        key = (start, end)
        cached = root_memo.get(key)
        if cached is not None:
            return cached
        count = end - start
        if count == 1:
            result = leaf_hash(leaves[start])
        else:
            k = _largest_pow2_less_than(count)
            result = node_hash(mth(start, start + k), mth(start + k, end))
        root_memo[key] = result
        return result

    path: list[bytes] = []

    def build(m: int, start: int, end: int) -> None:
        count = end - start
        if count == 1:
            return  # PATH(0, {d0}) = {}
        k = _largest_pow2_less_than(count)
        if m < k:
            # PATH(m, D[0:k]) : MTH(D[k:n])
            build(m, start, start + k)
            path.append(mth(start + k, end))
        else:
            # PATH(m - k, D[k:n]) : MTH(D[0:k])
            build(m - k, start + k, end)
            path.append(mth(start, start + k))

    build(leaf_index, 0, n)
    return path


def verify_inclusion(
    leaf_hash_value: bytes,
    leaf_index: int,
    tree_size: int,
    proof: list[bytes],
    root_hash: bytes,
) -> bool:
    """验证包含证明（RFC 9162 §2.1.6.2 伪代码的逐行翻译）。

    参数
    ----
    leaf_hash_value:
        被声称包含在树中的叶子哈希（HASH(0x00 || data)）。
    leaf_index:
        叶子在树中的下标 m。
    tree_size:
        声称的树大小 n。
    proof:
        路径上的兄弟哈希列表（PATH(m, D[n])）。
    root_hash:
        比对的树根 MTH(D[n])。

    任何长度异常、下标越界、路径多出元素等情况一律返回 False，不抛异常。
    """
    # 基本类型/边界检查（拒绝伪造的畸形输入）。
    if not isinstance(leaf_index, int) or not isinstance(tree_size, int):
        return False
    if isinstance(leaf_index, bool) or isinstance(tree_size, bool):
        return False
    if leaf_index < 0 or tree_size <= 0:
        return False
    if leaf_index >= tree_size:
        return False  # RFC 步骤 1
    if (
        len(leaf_hash_value) != HASH_SIZE
        or len(root_hash) != HASH_SIZE
    ):
        return False
    if any(not isinstance(p, bytes) or len(p) != HASH_SIZE for p in proof):
        return False

    # RFC 步骤 2：fn = leaf_index，sn = tree_size - 1。
    fn = leaf_index
    sn = tree_size - 1

    # RFC 步骤 3：r = hash。
    r = leaf_hash_value

    # RFC 步骤 4：依次消费路径中的每个兄弟节点。
    for p in proof:
        # 若 sn 已经为 0 却还有路径元素，路径过长，失败。
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            # LSB(fn) 置位，或 fn == sn：p 在 r 左边，r = HASH(0x01 || p || r)。
            r = node_hash(p, r)
            if not (fn & 1):
                # LSB(fn) 未置位（即因 fn == sn 进入此分支）：
                # 同步右移 fn、sn，直到 LSB(fn) 置位或 fn 为 0。
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            # 否则 p 在 r 右边，r = HASH(0x01 || r || p)。
            r = node_hash(r, p)
        # 最后两者各右移一位。
        fn >>= 1
        sn >>= 1

    # RFC 步骤 5：sn == 0 且 r == root_hash 才算通过。
    return sn == 0 and r == root_hash


def consistency_proof(first_size: int, leaves: list[bytes]) -> list[bytes]:
    """生成一致性证明 PROOF(first_size, D[n])。

    证明「大小为 first_size 的旧树是当前树（大小 n）的前缀」，
    即当前树没有被重新排序、也没有插入/替换早期叶子。

    要求 0 < first_size < n。first_size == n 的退化情形（恒真）由验证方处理。
    严格按 RFC 9162 §2.1.4.1 的 SUBPROOF 递归定义实现。
    """
    n = len(leaves)
    if not (0 < first_size < n):
        raise ValueError(
            f"一致性证明要求 0 < first_size < n，实际 first={first_size}, n={n}"
        )

    memo: dict[tuple[int, int], bytes] = {}

    def mth(start: int, end: int) -> bytes:
        key = (start, end)
        cached = memo.get(key)
        if cached is not None:
            return cached
        count = end - start
        if count == 1:
            result = leaf_hash(leaves[start])
        else:
            k = _largest_pow2_less_than(count)
            result = node_hash(mth(start, start + k), mth(start + k, end))
        memo[key] = result
        return result

    path: list[bytes] = []

    def subproof(m: int, start: int, end: int, b: bool) -> None:
        """SUBPROOF(m, D, b)：b 表示是否沿最左脊（首次调用为 true）。"""
        count = end - start
        if m == count:
            # SUBPROOF(m, D_m, true) = {}
            # SUBPROOF(m, D_m, false) = {MTH(D_m)}
            if not b:
                path.append(mth(start, end))
            return
        k = _largest_pow2_less_than(count)
        if m <= k:
            # SUBPROOF(m, D[0:k], b) : MTH(D[k:n])
            subproof(m, start, start + k, b)
            path.append(mth(start + k, end))
        else:
            # SUBPROOF(m - k, D[k:n], false) : MTH(D[0:k])
            subproof(m - k, start + k, end, False)
            path.append(mth(start, start + k))

    subproof(first_size, 0, n, True)
    return path


def verify_consistency(
    first_size: int,
    second_size: int,
    proof: list[bytes],
    first_hash: bytes,
    second_hash: bytes,
) -> bool:
    """验证一致性证明（RFC 9162 §2.1.4.2 伪代码的逐行翻译）。

    参数
    ----
    first_size / second_size:
        旧树 / 新树叶子数，要求 0 < first <= second。
    first_hash / second_hash:
        旧树根 / 新树根。
    proof:
        :func:`consistency_proof` 生成的兄弟哈希列表。

    定义扩展：first_size == second_size 时，只有两树根相等才算一致
    （RFC 只定义严格小于的情形）。任何畸形输入一律返回 False。
    """
    if (
        not isinstance(first_size, int)
        or not isinstance(second_size, int)
        or isinstance(first_size, bool)
        or isinstance(second_size, bool)
    ):
        return False
    if first_size <= 0 or second_size <= 0:
        return False
    if first_size > second_size:
        return False
    if len(first_hash) != HASH_SIZE or len(second_hash) != HASH_SIZE:
        return False
    if any(not isinstance(c, bytes) or len(c) != HASH_SIZE for c in proof):
        return False

    # 退化情形：同一棵树。根相同即一致，且不需要路径；根不同则被篡改。
    if first_size == second_size:
        return first_hash == second_hash and len(proof) == 0

    # 以下对应 RFC §2.1.4.2（first < second）。
    path = list(proof)

    # 步骤 1：空路径一定失败。
    if len(path) == 0:
        return False

    # 步骤 2：first 恰为二次幂时，把 first_hash 预置到路径开头。
    # （生成器在最左脊上省略了「旧树根」这一冗余节点。）
    if first_size & (first_size - 1) == 0:
        path = [first_hash] + path

    # 步骤 3：fn = first - 1，sn = second - 1。
    fn = first_size - 1
    sn = second_size - 1

    # 步骤 4：若 LSB(fn) 置位，同步右移直到 LSB(fn) 不置位。
    while fn & 1:
        fn >>= 1
        sn >>= 1

    # 步骤 5：fr、sr 均取路径第一个值。
    fr = sr = path[0]

    # 步骤 6：依次消费路径后续值，分别重建旧根 fr 与新根 sr。
    for c in path[1:]:
        if sn == 0:
            return False
        if fn & 1 or fn == sn:
            fr = node_hash(c, fr)
            sr = node_hash(c, sr)
            if not (fn & 1):
                while not (fn & 1) and fn != 0:
                    fn >>= 1
                    sn >>= 1
        else:
            sr = node_hash(sr, c)
        fn >>= 1
        sn >>= 1

    # 步骤 7：重建出的旧根、新根都匹配，且 sn == 0。
    return fr == first_hash and sr == second_hash and sn == 0
