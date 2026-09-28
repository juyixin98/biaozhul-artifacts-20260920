"""Eigendecomposition kernel.

Algorithm (real symmetric A):

1. Householder reduction to symmetric tridiagonal form T = Q^T A Q,
   accumulating the orthogonal factor Q.
2. Implicit QR iteration on T with a Wilkinson shift, chasing the bulge with
   Givens rotations; eigenvalues peel off from the trailing index whenever an
   off-diagonal element deflates.
3. Sort eigenvalues ascending and permute the accumulated eigenvectors.

The kernel raises :class:`NonConvergenceError` when a block exhausts its
sweep budget. *Stopping the loop is never treated as convergence*: an
eigenvalue is considered converged only when the corresponding off-diagonal
element is below a relative threshold, and independent residual evidence is
computed later by the evidence layer (the kernel does not certify itself).
"""

from dataclasses import dataclass, field, replace

import numpy as np

from sym_eig.config import FLOAT64_EPS


@dataclass(frozen=True)
class KernelResult:
    eigenvalues: np.ndarray          # shape (n,), ascending
    eigenvectors: np.ndarray         # shape (n, n), columns match eigenvalues
    householder_steps: int
    qr_sweeps: int                   # total implicit-QR sweeps performed
    max_block_sweeps: int            # worst-case sweeps spent on one block
    sweeps_per_eigenvalue: tuple[int, ...]
    final_offdiag_max: float         # max |off-diagonal| after deflation
    converged: bool = True


class NonConvergenceError(Exception):
    """Raised when the QR budget is exhausted on an unreduced block."""

    def __init__(
        self,
        block: tuple[int, int],
        sweeps: int,
        partial_eigenvalues: np.ndarray,
        residual_offdiag: float,
    ) -> None:
        self.block = block
        self.sweeps = sweeps
        self.partial_eigenvalues = partial_eigenvalues
        self.residual_offdiag = residual_offdiag
        super().__init__(
            f"implicit QR did not converge on block [{block[0]}:{block[1] + 1}] "
            f"after {sweeps} sweeps (residual off-diagonal {residual_offdiag:.3e})"
        )


@dataclass
class _Tridiagonal:
    diagonal: np.ndarray
    offdiagonal: np.ndarray             # e[i] = T[i, i+1]; e[n-1] = 0
    orthogonal: np.ndarray
    steps: int


def householder_tridiagonal(matrix: np.ndarray) -> _Tridiagonal:
    """Reduce a symmetric matrix to tridiagonal form in place on a copy."""
    n = matrix.shape[0]
    tri = np.array(matrix, dtype=np.float64, copy=True)
    q_acc = np.eye(n)
    steps = 0
    for k in range(n - 2):
        x = tri[k + 1:, k].copy()
        x_norm = float(np.linalg.norm(x))
        if x_norm == 0.0:
            continue
        alpha = -np.copysign(x_norm, x[0])
        v = x
        v[0] -= alpha
        v_norm_sq = float(v @ v)
        beta = 2.0 / v_norm_sq
        block = tri[k + 1:, k + 1:]
        p = beta * (block @ v)
        # rank-2 update: B' = B - v p^T - p v^T + beta (p^T v) v v^T
        tri[k + 1:, k + 1:] = (
            block - np.outer(v, p) - np.outer(p, v)
            + beta * float(p @ v) * np.outer(v, v)
        )
        tri[k + 1, k] = alpha
        tri[k, k + 1] = alpha
        tri[k + 2:, k] = 0.0
        tri[k, k + 2:] = 0.0
        qv = q_acc[:, k + 1:] @ v
        q_acc[:, k + 1:] -= beta * np.outer(qv, v)
        steps += 1
    diagonal = np.diag(tri).copy()
    offdiagonal = np.zeros(n)
    offdiagonal[:n - 1] = np.diag(tri, 1)
    return _Tridiagonal(diagonal, offdiagonal, q_acc, steps)


def _rotate_vectors(z: np.ndarray, p: int, c: float, s: float) -> None:
    """Right-multiply columns p,p+1 by the Givens matrix [[c,-s],[s,c]]."""
    first = z[:, p].copy()
    z[:, p] = c * first - s * z[:, p + 1]
    z[:, p + 1] = s * first + c * z[:, p + 1]


def implicit_wilkinson_qr(
    diagonal: np.ndarray,
    offdiagonal: np.ndarray,
    q_householder: np.ndarray,
    max_iters: int,
    tol: float = FLOAT64_EPS,
) -> KernelResult:
    """Solve a symmetric tridiagonal eigenproblem by implicit Wilkinson QR.

    ``max_iters`` bounds the number of sweeps applied to any single unreduced
    block before :class:`NonConvergenceError` is raised.
    """
    d = np.array(diagonal, dtype=np.float64, copy=True)
    e = np.array(offdiagonal, dtype=np.float64, copy=True)
    z = np.array(q_householder, dtype=np.float64, copy=True)
    n = d.shape[0]
    if n == 1:
        return KernelResult(
            eigenvalues=d.copy(), eigenvectors=z, householder_steps=0,
            qr_sweeps=0, max_block_sweeps=0,
            sweeps_per_eigenvalue=(0,), final_offdiag_max=0.0,
        )

    end = n - 1
    total_sweeps = 0
    block_sweeps = 0
    max_block_sweeps = 0
    sweeps_per_eigenvalue: list[int] = [0] * n

    def _deflated(left: int, right: int) -> bool:
        return abs(e[left]) <= tol * (abs(d[left]) + abs(d[right]))

    while end > 0:
        # Find the top l of the unreduced block ending at `end`.
        l = end
        while l > 0 and not _deflated(l - 1, l):
            l -= 1
        if l == end:  # eigenvalue at `end` has converged
            sweeps_per_eigenvalue[end] = block_sweeps
            max_block_sweeps = max(max_block_sweeps, block_sweeps)
            block_sweeps = 0
            # Zero the bond that actually deflated (between end-1 and end),
            # so the reported final off-diagonal reflects exact deflation.
            if end > 0:
                e[end - 1] = 0.0
            end -= 1
            continue

        block_sweeps += 1
        if block_sweeps > max_iters:
            raise NonConvergenceError(
                block=(l, end),
                sweeps=block_sweeps - 1,
                partial_eigenvalues=d.copy(),
                residual_offdiag=float(max(
                    abs(e[i]) / max(1.0, abs(d[i]) + abs(d[i + 1]))
                    for i in range(l, end)
                )),
            )
        total_sweeps += 1

        # Wilkinson shift: eigenvalue of the trailing 2x2 closer to d[end].
        d_end = d[end]
        delta = (d[end - 1] - d_end) * 0.5
        e_end = e[end - 1]
        if delta == 0.0:
            shift = d_end - abs(e_end)
        else:
            root = np.hypot(delta, e_end)
            denominator = delta + np.copysign(root, delta)
            shift = d_end - e_end * e_end / denominator if denominator != 0.0 else d_end

        # First Givens rotation zeros (l+1, l) of (T - shift*I).
        x = d[l] - shift
        w = e[l]
        r = np.hypot(x, w)
        c, s = x / r, -w / r
        d_l, d_next, e_l = d[l], d[l + 1], e[l]
        d[l] = c * c * d_l - 2.0 * s * c * e_l + s * s * d_next
        d[l + 1] = s * s * d_l + 2.0 * s * c * e_l + c * c * d_next
        e[l] = s * c * (d_l - d_next) + (c * c - s * s) * e_l
        bulge = -s * e[l + 1] if l + 1 < end else 0.0
        if l + 1 < end:
            e[l + 1] *= c
        _rotate_vectors(z, l, c, s)

        # Chase the bulge down the tridiagonal band.
        u, v = e[l], bulge
        for p in range(l + 1, end):
            r = np.hypot(u, v)
            if r == 0.0:
                break
            c, s = u / r, -v / r
            e[p - 1] = r
            d_p, d_next, e_p = d[p], d[p + 1], e[p]
            d[p] = c * c * d_p - 2.0 * s * c * e_p + s * s * d_next
            d[p + 1] = s * s * d_p + 2.0 * s * c * e_p + c * c * d_next
            e[p] = s * c * (d_p - d_next) + (c * c - s * s) * e_p
            next_bulge = -s * e[p + 1] if p < end - 1 else 0.0
            if p < end - 1:
                e[p + 1] *= c
            _rotate_vectors(z, p, c, s)
            u, v = e[p], next_bulge

    sweeps_per_eigenvalue[0] = block_sweeps
    max_block_sweeps = max(max_block_sweeps, block_sweeps)

    order = np.argsort(d, kind="stable")
    ordered = KernelResult(
        eigenvalues=d[order],
        eigenvectors=z[:, order],
        householder_steps=0,
        qr_sweeps=total_sweeps,
        max_block_sweeps=max_block_sweeps,
        sweeps_per_eigenvalue=tuple(sweeps_per_eigenvalue[i] for i in order),
        final_offdiag_max=float(np.max(np.abs(e))),
    )
    return ordered


def symmetric_eigen(matrix: np.ndarray, max_iters: int,
                    tol: float = FLOAT64_EPS) -> KernelResult:
    """Full pipeline: Householder tridiagonalization then implicit QR."""
    tridiagonal = householder_tridiagonal(matrix)
    result = implicit_wilkinson_qr(
        tridiagonal.diagonal,
        tridiagonal.offdiagonal,
        tridiagonal.orthogonal,
        max_iters=max_iters,
        tol=tol,
    )
    return replace(result, householder_steps=tridiagonal.steps)
