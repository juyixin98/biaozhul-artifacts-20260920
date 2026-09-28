import pytest

from krylov_expm.budget import check_budget, estimate_basis_bytes
from krylov_expm.config import SolverConfig
from krylov_expm.errors import ExpmvFailure, FailureCategory


def test_basis_storage_estimate_counts_vectors():
    basis, hessenberg = estimate_basis_bytes(dimension=1000, max_krylov_dim=30)
    assert basis == 31 * 1000 * 8
    assert hessenberg == 31 * 30 * 8


def test_budget_exceeded_raises_with_details():
    cfg = SolverConfig(max_basis_bytes=1024)
    with pytest.raises(ExpmvFailure) as excinfo:
        check_budget(1000, cfg)
    assert excinfo.value.category is FailureCategory.BUDGET_EXCEEDED
    assert excinfo.value.details["total_bytes"] > 1024
    assert excinfo.value.details["limit_bytes"] == 1024


def test_dimension_limit_raises():
    cfg = SolverConfig(max_dimension=10)
    with pytest.raises(ExpmvFailure) as excinfo:
        check_budget(11, cfg)
    assert excinfo.value.category is FailureCategory.BUDGET_EXCEEDED


def test_budget_within_limit_returns_report():
    report = check_budget(100, SolverConfig())
    assert report.total_bytes <= report.limit_bytes
