"""Elimination tree of a sparse symmetric matrix.

The elimination tree (etree) of the Cholesky factor is the structural backbone
of symbolic factorization: ``parent[j]`` is the smallest row index ``i > j``
for which ``L[i, j]`` is nonzero.  It is computed from the sparsity pattern
*before* any numeric work, using the classic path-compression algorithm of
Liu (1986); only sparse index arrays are touched.
"""
from __future__ import annotations

import numpy as np
from scipy import sparse

#: Sentinel meaning "no parent" (root node / forest).
NO_PARENT = -1


def elimination_tree(matrix_csc: sparse.csc_matrix) -> np.ndarray:
    r"""Compute the Cholesky elimination tree.

    The tree is defined by

        ``parent[i] = min { k > i : L[k, i] != 0 }``

    (or ``-1`` if no such k exists), so every edge points from a smaller
    index to a larger one.  This is the classic path-compression algorithm
    of Liu (1986), as used in CSparse: scan columns left to right and, for
    every lower entry ``A[k, i]`` with ``i < k``, link the root of ``i``'s
    current ancestor chain to ``k``.

    Parameters
    ----------
    matrix_csc:
        Symmetric matrix in CSC form; entries ``i < k`` in column ``k`` are
        consumed (equivalently the lower triangle, read across columns).

    Returns
    -------
    numpy.ndarray
        ``parent[i]`` is the parent of node ``i`` (``-1`` for a root).
    """
    n = matrix_csc.shape[0]
    parent = np.full(n, NO_PARENT, dtype=np.int64)
    ancestor = np.full(n, NO_PARENT, dtype=np.int64)

    indptr, indices = matrix_csc.indptr, matrix_csc.indices
    for k in range(n):
        for idx in range(indptr[k], indptr[k + 1]):
            i = int(indices[idx])
            # Walk the ancestor chain of every neighbour i < k.
            while i != NO_PARENT and i < k:
                a_i = int(ancestor[i])
                if a_i == NO_PARENT:
                    parent[i] = k
                    ancestor[i] = k
                    break
                ancestor[i] = k  # path compression
                i = a_i
    return parent


def tree_height(parent: np.ndarray) -> int:
    """Number of edges on the longest root-to-leaf path in the etree.

    Iterative with memoization so a chain of tens of thousands of nodes does
    not exhaust Python's recursion limit.
    """
    depth = np.full(parent.size, -1, dtype=np.int64)
    for start in range(parent.size):
        if depth[start] != -1:
            continue
        path: list[int] = []
        node = start
        while node != NO_PARENT and depth[node] == -1:
            path.append(node)
            node = int(parent[node])
        base = 0 if node == NO_PARENT else int(depth[node]) + 1
        for d, node in enumerate(reversed(path)):
            depth[node] = base + d
    return int(depth.max(initial=0))


def postorder(parent: np.ndarray) -> np.ndarray:
    """Return a postordering of the elimination tree forest.

    Children are visited before their parent; each tree root is visited in
    ascending index order.  The result is a permutation of ``0..n-1``.
    """
    n = parent.size
    children: list[list[int]] = [[] for _ in range(n)]
    for child, p in enumerate(parent):
        if p != NO_PARENT:
            children[int(p)].append(child)

    order: list[int] = []
    for root in range(n):
        if parent[root] != NO_PARENT:
            continue
        stack: list[tuple[int, bool]] = [(root, False)]
        while stack:
            node, expanded = stack.pop()
            if expanded:
                order.append(node)
                continue
            stack.append((node, True))
            for child in reversed(children[node]):
                stack.append((child, False))
    return np.asarray(order, dtype=np.int64)
