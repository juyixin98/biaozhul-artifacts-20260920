"""Generate the synthetic matrix/vector fixtures used by tests and examples.

Run from the repo root:  python fixtures/generate_fixtures.py
All matrices are small, deterministic (fixed seed), and fully synthetic;
no production accounts or real business data are involved.
"""
import json
from pathlib import Path

import numpy as np
import scipy.sparse as sp

OUT = Path(__file__).resolve().parent
SEED = 20260927


def _save(manifest, name, matrix, vector, description):
    coo = matrix.tocoo()
    np.savez(
        OUT / f"{name}.npz",
        row=coo.row,
        col=coo.col,
        data=coo.data,
        shape=np.array(coo.shape),
        vector=vector,
    )
    manifest.append(
        {
            "name": name,
            "file": f"{name}.npz",
            "description": description,
            "shape": list(coo.shape),
            "nnz": int(coo.nnz),
        }
    )


def main():
    rng = np.random.default_rng(SEED)
    manifest = []

    n = 8
    lap = sp.diags(
        [-np.ones(n - 1), 2.0 * np.ones(n), -np.ones(n - 1)], [-1, 0, 1], format="csr"
    )
    _save(
        manifest, "symmetric_tridiag", lap, rng.standard_normal(n),
        "Symmetric positive definite 1D Laplacian (n=8); normal, well-conditioned case.",
    )

    n = 10
    grcar = sp.diags(
        [-np.ones(n - 1), np.ones(n), np.ones(n - 1), np.ones(n - 2), np.ones(n - 3)],
        [-1, 0, 1, 2, 3],
        format="csr",
    )
    _save(
        manifest, "grcar_nonnormal", grcar, rng.standard_normal(n),
        "Grcar matrix (n=10): highly non-normal Toeplitz; stresses Arnoldi error estimates.",
    )

    n = 8
    jordan = sp.diags([0.5 * np.ones(n), np.ones(n - 1)], [0, 1], format="csr")
    _save(
        manifest, "jordan_block", jordan, rng.standard_normal(n),
        "Single Jordan block (eigenvalue 0.5, n=8): defective matrix, polynomial growth.",
    )

    n = 12
    lap12 = sp.diags(
        [-np.ones(n - 1), 2.0 * np.ones(n), -np.ones(n - 1)], [-1, 0, 1], format="csr"
    )
    _save(
        manifest, "decay_longtime", -lap12, rng.standard_normal(n),
        "Negative Laplacian (n=12): decaying semigroup, used for long-time (t=25) tests.",
    )

    (OUT / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"wrote {len(manifest)} fixtures to {OUT}")


if __name__ == "__main__":
    main()
