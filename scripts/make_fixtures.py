"""Generate the local synthetic fixtures under fixtures/matrices/.

All fixtures are deterministic (fixed seeds, closed-form constructions) so
the test-suite and the mpmath reference values are reproducible from this
script alone.  No external data is involved.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import scipy.sparse as sp

OUT = Path(__file__).resolve().parent.parent / "fixtures" / "matrices"


def dump(name: str, description: str, A: sp.spmatrix, v: np.ndarray, t: float, tol: float) -> None:
    coo = A.tocoo()
    payload = {
        "name": name,
        "description": description,
        "n": A.shape[0],
        "row": [int(i) for i in coo.row],
        "col": [int(j) for j in coo.col],
        "data": [float(x) for x in coo.data],
        "vector": [float(x) for x in v],
        "t": float(t),
        "tol": float(tol),
    }
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(payload, indent=2))
    print(f"wrote {path}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    # 1. diagonal 3x3: closed-form answer exp(t*lambda_i) * v_i
    A = sp.diags([-1.0, -0.5, 0.25], format="csr")
    v = np.array([1.0, -2.0, 0.5])
    dump("diag3", "diagonal 3x3, closed-form reference", A, v, t=1.25, tol=1e-12)

    # 2. single Jordan block J(4, lambda): defective, nilpotent superdiagonal
    lam = -0.7
    J = np.diag(np.full(4, lam)) + np.diag(np.ones(3), 1)
    v = np.array([1.0, 0.5, -0.25, 2.0])
    dump(
        "jordan4",
        "single 4x4 Jordan block (defective eigenvalue -0.7)",
        sp.csr_matrix(J), v, t=1.5, tol=1e-12,
    )

    # 3. non-normal 8x8: Grcar-like Toeplitz, strong transient growth
    N = 8
    diagonals = {
        -1: -np.ones(N - 1),
        0: np.full(N, -0.2),
        1: np.ones(N - 1),
        2: np.ones(N - 2),
        3: np.ones(N - 3),
    }
    G = sp.diags(list(diagonals.values()), list(diagonals.keys()), format="csr")
    rng = np.random.default_rng(20260927)
    v = rng.standard_normal(N)
    dump(
        "nonnormal8",
        "Grcar-like 8x8 Toeplitz, highly non-normal, transient growth",
        G, v, t=2.0, tol=1e-10,
    )

    # 4. long-time integration: skew-symmetric advection generator, n=60.
    # Purely imaginary spectrum (no decay), ||A|| ~ 1, t = 120 -> t*||A||
    # far beyond what one m_max=30 Krylov step can cover, forcing genuine
    # time segmentation and restarts.  n=60 > m_max also rules out
    # full-space happy breakdown.
    N = 60
    upper = 0.5 * np.ones(N - 1)
    A_long = sp.diags([-upper, upper], [-1, 1], format="csr")
    x = np.linspace(0.0, 1.0, N)
    v = np.exp(-((x - 0.3) ** 2) / 0.01)
    dump(
        "advection60_long",
        "skew-symmetric advection generator n=60 integrated to t=120 "
        "(imaginary spectrum, requires time segmentation)",
        A_long, v, t=120.0, tol=1e-9,
    )

    # 5. rotation generator: pure imaginary spectrum, no decay, t negative use
    R = sp.csr_matrix(np.array([[0.0, -1.0, 0.0], [1.0, 0.0, 0.0], [0.0, 0.0, -0.1]]))
    v = np.array([1.0, 0.0, 3.0])
    dump(
        "rotation3",
        "rotation generator plus damped mode; used with t < 0 in tests",
        R, v, t=-2.0, tol=1e-12,
    )


if __name__ == "__main__":
    main()
