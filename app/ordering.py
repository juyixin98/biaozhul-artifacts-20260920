"""Deterministic root ordering and conjugate pairing.

Ordering contract:
- Output order is a total, deterministic order: sort by (real, imag) at full
  precision. It does not depend on the order the kernel produced the roots,
  so replays and permuted inputs give byte-identical orderings.
- Conjugate pairing is a separate, explicit step with a caller-visible
  tolerance ``pair_tol``: roots z, w form a conjugate pair when
  |z - conj(w)| <= pair_tol * max(1, |z|, |w|). Matching is greedy in
  ascending distance after sorting candidates deterministically. Roots with
  no partner within the tolerance are reported as ``unpaired`` — pairing is
  never forced.
"""

from __future__ import annotations

from typing import List, Tuple

import numpy as np


def order_indices(roots: np.ndarray) -> List[int]:
    """Deterministic total order by (real, imag)."""
    return sorted(range(len(roots)), key=lambda i: (roots[i].real, roots[i].imag))


def conjugate_pairs(
    roots: np.ndarray,
    pair_tol: float,
    ordered: List[int],
) -> Tuple[List[List[int]], List[int]]:
    """Greedy conjugate matching under an explicit relative tolerance.

    Returns (pairs, unpaired) with indices into the *ordered* output array.
    """
    n = len(ordered)
    remaining = set(range(n))
    pairs: List[List[int]] = []
    unpaired: List[int] = []

    for pos in range(n):
        if pos not in remaining:
            continue
        remaining.discard(pos)
        z = roots[ordered[pos]]
        best = None
        best_dist = np.inf
        for q in sorted(remaining):
            w = roots[ordered[q]]
            dist = abs(z - np.conj(w))
            if dist < best_dist:
                best_dist = dist
                best = q
        if best is not None:
            w = roots[ordered[best]]
            limit = pair_tol * max(1.0, abs(z), abs(w))
            if best_dist <= limit:
                remaining.discard(best)
                pairs.append([pos, best])
                continue
        unpaired.append(pos)

    return pairs, unpaired
