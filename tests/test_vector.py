"""SparseVector semantics: duplicate merging, zero handling, validation."""

import math

import pytest

from sparse_retrieval import SparseVector


def test_duplicate_dimensions_are_summed():
    v = SparseVector([(3, 1.5), (1, 2.0), (3, 0.5)])
    assert v.as_dict() == {1: 2.0, 3: 2.0}
    assert v.duplicates_merged == 1


def test_duplicate_dimensions_cancelling_to_zero_are_dropped():
    v = SparseVector([(2, 1.0), (2, -1.0), (5, 3.0)])
    assert v.as_dict() == {5: 3.0}
    assert v.nnz == 1


def test_zero_weight_entries_are_dropped():
    v = SparseVector([(1, 0.0), (2, 4.0)])
    assert v.as_dict() == {2: 4.0}


def test_negative_weights_are_kept():
    v = SparseVector([(1, -2.5), (2, 1.0)])
    assert v.as_dict() == {1: -2.5, 2: 1.0}
    assert math.isclose(v.norm, math.sqrt(2.5**2 + 1.0))


def test_zero_vector_semantics():
    v = SparseVector([])
    assert v.is_zero
    assert v.norm == 0.0
    assert v.nnz == 0
    # Entries that cancel out also produce the zero vector.
    assert SparseVector([(1, 1.0), (1, -1.0)]).is_zero


def test_entries_sorted_by_dimension():
    v = SparseVector([(9, 1.0), (2, 1.0), (5, 1.0)])
    assert [d for d, _ in v.entries] == [2, 5, 9]


def test_int_weights_accepted_and_converted():
    v = SparseVector([(1, 2)])
    assert v.as_dict() == {1: 2.0}


@pytest.mark.parametrize(
    "pairs",
    [
        [(-1, 1.0)],            # negative dimension
        [(1.5, 1.0)],           # non-integer dimension
        [(1, float("nan"))],    # NaN weight
        [(1, float("inf"))],    # infinite weight
        [("1", 1.0)],           # string dimension
        [(1, "x")],             # string weight
        [(True, 1.0)],          # bool dimension
        [(1, True)],            # bool weight
    ],
)
def test_invalid_input_rejected(pairs):
    with pytest.raises(ValueError):
        SparseVector(pairs)


def test_vectors_are_immutable_value_objects():
    v1 = SparseVector([(1, 1.0)])
    v2 = SparseVector([(1, 1.0)])
    assert v1 == v2
    with pytest.raises(AttributeError):
        v1.norm = 3.0  # type: ignore[misc]
