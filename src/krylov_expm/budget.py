"""Resource budget checks, including Krylov basis vector storage."""
from __future__ import annotations

from dataclasses import asdict, dataclass

from .config import SolverConfig
from .errors import ExpmvFailure, FailureCategory

_FLOAT64_BYTES = 8


@dataclass(frozen=True)
class BudgetReport:
    dimension: int
    max_krylov_dim: int
    basis_vectors_bytes: int
    hessenberg_bytes: int
    total_bytes: int
    limit_bytes: int

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_basis_bytes(dimension: int, max_krylov_dim: int) -> tuple[int, int]:
    """Bytes for the Arnoldi basis (m+1 vectors of length n) and Hessenberg."""
    basis = (max_krylov_dim + 1) * dimension * _FLOAT64_BYTES
    hessenberg = (max_krylov_dim + 1) * max_krylov_dim * _FLOAT64_BYTES
    return basis, hessenberg


def check_budget(dimension: int, config: SolverConfig) -> BudgetReport:
    """Reject requests whose Krylov working set exceeds the configured budget."""
    basis, hessenberg = estimate_basis_bytes(dimension, config.max_krylov_dim)
    report = BudgetReport(
        dimension=dimension,
        max_krylov_dim=config.max_krylov_dim,
        basis_vectors_bytes=basis,
        hessenberg_bytes=hessenberg,
        total_bytes=basis + hessenberg,
        limit_bytes=config.max_basis_bytes,
    )
    if dimension > config.max_dimension:
        raise ExpmvFailure(
            FailureCategory.BUDGET_EXCEEDED,
            f"matrix dimension {dimension} exceeds limit {config.max_dimension}",
            details=report.to_dict(),
        )
    if report.total_bytes > report.limit_bytes:
        raise ExpmvFailure(
            FailureCategory.BUDGET_EXCEEDED,
            f"Krylov basis storage {report.total_bytes} bytes exceeds "
            f"budget {report.limit_bytes} bytes",
            details=report.to_dict(),
        )
    return report
