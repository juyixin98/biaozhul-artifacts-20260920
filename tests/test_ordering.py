"""Ordering and conjugate-pairing tests."""

import numpy as np

from app.ordering import conjugate_pairs, order_indices


class TestOrdering:
    def test_permutation_invariant(self):
        roots = np.array([3.0, 1.0 + 1j, 1.0 - 1j, -2.0, 1.0 + 0j], dtype=np.complex128)
        rng = np.random.default_rng(42)
        base = order_indices(roots)
        base_sorted = [roots[i] for i in base]
        for _ in range(20):
            perm = rng.permutation(len(roots))
            shuffled = roots[perm]
            assert [shuffled[i] for i in order_indices(shuffled)] == base_sorted

    def test_sort_key_is_real_then_imag(self):
        roots = np.array([1.0 + 2j, 1.0 - 2j, -5.0 + 0j], dtype=np.complex128)
        order = order_indices(roots)
        assert [complex(roots[i]) for i in order] == [-5.0 + 0j, 1.0 - 2j, 1.0 + 2j]


class TestConjugatePairs:
    def test_exact_conjugates_paired(self):
        roots = np.array([1.0 + 2j, 1.0 - 2j, 3.0 + 0j], dtype=np.complex128)
        order = order_indices(roots)
        pairs, unpaired = conjugate_pairs(roots, 1e-8, order)
        assert pairs == [[0, 1]]
        assert unpaired == [2]

    def test_tolerance_is_explicit(self):
        # Pair 1+2j / 1-2.001j: matched only when pair_tol is loose enough.
        roots = np.array([1.0 + 2.0j, 1.0 - 2.001j], dtype=np.complex128)
        order = order_indices(roots)
        pairs, unpaired = conjugate_pairs(roots, 1e-6, order)
        assert pairs == [] and unpaired == [0, 1]
        pairs, unpaired = conjugate_pairs(roots, 1e-2, order)
        assert pairs == [[0, 1]] and unpaired == []

    def test_real_roots_not_forced_into_pairs(self):
        roots = np.array([1.0, 2.0, 3.0], dtype=np.complex128)
        order = order_indices(roots)
        pairs, unpaired = conjugate_pairs(roots, 1e-8, order)
        assert pairs == []
        assert unpaired == [0, 1, 2]
