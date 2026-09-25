"""Tests for the synthetic pipeline and the NumPy logistic regression."""
import numpy as np

from grouped_splitter.model import LogisticRegression
from grouped_splitter.pipeline import run_synthetic_demo, verify_invariants
from grouped_splitter.splitter import split_dataset
from grouped_splitter.synthetic import generate_synthetic_dataset


def test_logistic_regression_learns_linearly_separable_data():
    rng = np.random.default_rng(0)
    n = 600
    X = rng.normal(0, 1, size=(n, 4))
    true_w = np.array([[2.0, -1.0], [-1.5, 1.0], [1.0, 2.0], [-2.0, 0.5]])
    y = np.argmax(X @ true_w + rng.normal(0, 0.3, size=(n, 2)), axis=1)
    model = LogisticRegression(n_epochs=200, lr=0.5, seed=1).fit(X, y)
    assert model.accuracy(X, y) > 0.8
    assert model.loss_history[-1] < model.loss_history[0]


def test_model_is_deterministic():
    rng = np.random.default_rng(5)
    X = rng.normal(size=(200, 3))
    y = (X[:, 0] + X[:, 1] > 0).astype(int)
    m1 = LogisticRegression(seed=9).fit(X, y)
    m2 = LogisticRegression(seed=9).fit(X, y)
    assert np.array_equal(m1.W, m2.W)


def test_full_demo_checks_pass():
    report = run_synthetic_demo(
        n_samples=800, n_groups=25, seed=42, tolerance=0.05
    )
    checks = report["checks"]
    assert checks["total_samples_conserved"] is True
    assert checks["splits_pairwise_disjoint"] is True
    assert checks["groups_isolated_to_one_split"] is True
    assert checks["all_groups_assigned"] is True
    assert checks["reported_sizes_consistent"] is True
    assert checks["leaked_groups"] == []
    # Rare class is structurally reported.
    assert "0" in report["split"]["rare_classes"]


def test_verify_invariants_detects_duplicate_assignment():
    """The independent verifier must catch a corrupted partition."""
    dataset = generate_synthetic_dataset(n_samples=200, n_groups=10, seed=1)
    result = split_dataset(
        list(dataset.group_ids), list(dataset.y), seed=1
    )
    # Tamper: duplicate an index into another split -> conservation fails.
    victim = result.split_names[1]
    object.__setattr__(
        result,
        "assignments",
        {**result.assignments, victim: result.assignments[victim] + [0]},
    )
    checks = verify_invariants(dataset, result)
    assert checks["total_samples_conserved"] is False


def test_demo_model_metrics_present():
    report = run_synthetic_demo(n_samples=400, n_groups=15, seed=3)
    model = report["model"]
    assert model["type"] == "numpy_multinomial_logistic_regression"
    assert model["metrics"]["train"]["accuracy"] is not None
    assert model["final_loss"] <= model["initial_loss"]


def test_predict_before_fit_raises():
    import pytest
    with pytest.raises(RuntimeError, match="not fitted"):
        LogisticRegression().predict(np.zeros((2, 2)))
