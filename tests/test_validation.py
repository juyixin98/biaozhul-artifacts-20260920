"""Input validation and error-path tests."""
import numpy as np
import pytest

from grouped_splitter import split_dataset


def test_empty_input_raises():
    with pytest.raises(ValueError, match="empty"):
        split_dataset([], [])


def test_length_mismatch_raises():
    with pytest.raises(ValueError, match="equal length"):
        split_dataset(["a", "b"], [0])


def test_ratios_must_sum_to_one():
    with pytest.raises(ValueError, match="sum to 1"):
        split_dataset(["a"] * 4, [0] * 4, ratios=(0.5, 0.4))


def test_ratios_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        split_dataset(["a"] * 4, [0] * 4, ratios=(0.8, 0.0, 0.2))


def test_duplicate_split_names_rejected():
    with pytest.raises(ValueError, match="unique"):
        split_dataset(
            ["a"] * 4, [0] * 4,
            ratios=(0.5, 0.5),
            split_names=["same", "same"],
        )


def test_mixed_labels_inside_group_are_supported():
    """A group may mix classes; all its rows still move atomically."""
    groups = ["a", "a", "a", "b", "b"]
    labels = [0, 1, 2, 0, 1]
    result = split_dataset(groups, labels, ratios=(0.6, 0.4), seed=1)
    owner = result.group_assignments
    assert len(set(owner.values())) >= 1
    # All three rows of "a" are in the same split.
    a_indices = [i for i, g in enumerate(groups) if g == "a"]
    splits_with_a = {
        s for s in result.split_names
        if set(a_indices) & set(result.assignments[s])
    }
    assert len(splits_with_a) == 1
    # Class counts of group "a" are reported on its group row.
    report_a = next(g for g in result.group_reports if g.group_id == "a")
    assert report_a.class_counts == {"0": 1, "1": 1, "2": 1}


def test_bad_tolerance_rejected():
    with pytest.raises(ValueError, match="tolerance"):
        split_dataset(["a"] * 4, [0] * 4, tolerance=0)


def test_unhashable_input_rejected():
    with pytest.raises(ValueError, match="hashable"):
        split_dataset([["a"], ["b"]], [0, 1])


def test_mapping_ratios_supported():
    result = split_dataset(
        ["a"] * 6 + ["b"] * 4, [0] * 6 + [1] * 4,
        ratios={"tr": 0.6, "te": 0.4},
    )
    assert result.split_names == ["tr", "te"]
    assert result.ratios == {"tr": 0.6, "te": 0.4}


def test_integer_group_ids_work():
    groups = np.repeat(np.arange(20), 5)
    labels = np.tile([0, 1], 50)
    result = split_dataset(list(groups), list(labels), seed=11)
    assert result.n_groups == 20
