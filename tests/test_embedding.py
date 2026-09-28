"""Embedding invariants: minimal length, no aliasing, shared-element rule."""

import numpy as np
import pytest

from toeplitz_fft.embedding import (
    check_shared_element,
    circulant_first_column,
    embedding_length,
    validate_first_column_row,
)
from toeplitz_fft.errors import InconsistentToeplitzError, ShapeMismatchError


def test_minimal_embedding_length_is_m_plus_n_minus_1():
    assert embedding_length(3, 2) == 4
    assert embedding_length(1, 1) == 1
    assert embedding_length(7, 5) == 11  # deliberately not a power of two


def test_power_of_two_padding_never_shrinks_below_minimum():
    assert embedding_length(5, 5, pad_to_power_of_two=True) == 16  # 9 -> 16
    assert embedding_length(4, 4, pad_to_power_of_two=True) == 8   # 7 -> 8
    assert embedding_length(1, 1, pad_to_power_of_two=True) == 1


def test_embedding_length_rejects_zero_dimensions():
    with pytest.raises(ShapeMismatchError):
        embedding_length(0, 3)
    with pytest.raises(ShapeMismatchError):
        embedding_length(3, 0)


def test_circulant_first_column_hand_example():
    # c = [1, 2, 3], r = [1, 4], L = 4 -> [1, 2, 3, 4]
    c = np.array([1.0, 2.0, 3.0])
    r = np.array([1.0, 4.0])
    col = circulant_first_column(c, r, embedding_length(3, 2))
    np.testing.assert_array_equal(col, np.array([1.0, 2.0, 3.0, 4.0]))


def test_circulant_first_column_with_gap_zeros():
    # m=2, n=2, L=6 (padded): [c0, c1, 0, 0, 0, r1]
    c = np.array([5.0, 6.0])
    r = np.array([5.0, 7.0])
    col = circulant_first_column(c, r, 6)
    np.testing.assert_array_equal(col, np.array([5.0, 6.0, 0.0, 0.0, 0.0, 7.0]))


def test_circulant_first_column_rejects_too_small_L():
    c = np.array([1.0, 2.0, 3.0])
    r = np.array([1.0, 4.0])
    with pytest.raises(ShapeMismatchError):
        circulant_first_column(c, r, 3)  # need >= 4


def test_shared_element_mismatch_raises_typed_error():
    with pytest.raises(InconsistentToeplitzError) as excinfo:
        check_shared_element(1.0, 1.5, rtol=1e-9, atol=1e-12)
    assert excinfo.value.category == "inconsistent_shared_element"


def test_shared_element_within_tolerance_accepted():
    check_shared_element(1.0, 1.0 + 1e-13, rtol=1e-9, atol=1e-12)


def test_validate_rejects_non_1d_and_empty():
    with pytest.raises(ShapeMismatchError):
        validate_first_column_row(np.zeros((2, 2)), np.zeros(2), 1e-9, 1e-12)
    with pytest.raises(ShapeMismatchError):
        validate_first_column_row(np.zeros(0), np.zeros(0), 1e-9, 1e-12)
