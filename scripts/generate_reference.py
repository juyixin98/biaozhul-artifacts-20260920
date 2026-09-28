"""Generate high-precision reference roots for the test fixtures.

Independence contract: reference values are produced by mpmath.polyroots at
50 decimal digits — a different algorithm (Durand-Kerner with arbitrary
precision) from the service kernel (companion eigenvalues + Aberth in
float64). Fixture coefficients are computed with exact integer arithmetic.
The committed output (tests/fixtures/reference_roots.json) is the reference;
the service under test never generates it.

Usage: .venv/bin/python scripts/generate_reference.py
"""

from __future__ import annotations

import json
import os

from mpmath import mp, mpf, mpc, polyroots

mp.dps = 50

OUT_PATH = os.path.join(os.path.dirname(__file__), "..", "tests", "fixtures", "reference_roots.json")


def int_coeffs_from_roots(roots) -> list[int]:
    """Exact integer convolution for monic polynomials with integer roots."""
    coeffs = [1]
    for r in roots:
        nxt = [0] * (len(coeffs) + 1)
        for k, c in enumerate(coeffs):
            nxt[k] += c
            nxt[k + 1] -= c * r
        coeffs = nxt
    return coeffs


def solve_case(coeffs):
    """mpmath.polyroots takes the leading coefficient first — the same
    descending-power convention as the rest of this project, no reversal."""
    roots = polyroots([mpf(c) for c in coeffs], maxsteps=400, error=False)
    return sorted(roots, key=lambda z: (z.real, z.imag))


def exact_roots_case(true_roots):
    """Reference from analytically known roots (exact, no iteration)."""
    return [mpc(re, im) for re, im in true_roots]


def main() -> None:
    cases = {}

    # 1. Known real roots 1, 2, 3.
    coeffs = int_coeffs_from_roots([1, 2, 3])
    cases["cubic_123"] = {
        "coefficients": coeffs,
        "true_roots": [[1.0, 0.0], [2.0, 0.0], [3.0, 0.0]],
        "reference_roots": solve_case(coeffs),
    }

    # 2. Complex conjugate pair plus real roots: 1±2i, -1, 1/2.
    #    2*(z^2-2z+5)(z+1)(z-1/2) = 2z^4 - 3z^3 + 7z^2 + 7z - 5
    coeffs = [2, -3, 7, 7, -5]
    cases["quartic_complex"] = {
        "coefficients": coeffs,
        "true_roots": [[1.0, 2.0], [1.0, -2.0], [-1.0, 0.0], [0.5, 0.0]],
        "reference_roots": solve_case(coeffs),
    }

    # 3. Wilkinson-style: roots 1..8 (ill-conditioned). mpmath's Durand-Kerner
    #    does not converge on this even at 80 dps, and the roots are exactly
    #    the integers 1..8 by construction, so the reference is analytic.
    coeffs = int_coeffs_from_roots(list(range(1, 9)))
    cases["wilkinson_8"] = {
        "coefficients": coeffs,
        "true_roots": [[float(k), 0.0] for k in range(1, 9)],
        "reference_roots": exact_roots_case([[float(k), 0.0] for k in range(1, 9)]),
        "reference_source": "exact_analytic",
    }

    # 4. Near-multiple: (z-1)^4 (z-2). Durand-Kerner converges only linearly
    #    at multiple roots and mpmath gives up; the reference is the exact
    #    analytic root set instead (which is the honest ground truth here).
    coeffs = int_coeffs_from_roots([1, 1, 1, 1, 2])
    cases["near_multiple"] = {
        "coefficients": coeffs,
        "true_roots": [[1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [1.0, 0.0], [2.0, 0.0]],
        "reference_roots": exact_roots_case([[1.0, 0.0]] * 4 + [[2.0, 0.0]]),
        "reference_source": "exact_analytic",
    }

    # 5. High-order sparse: z^20 - 1.
    coeffs = [1] + [0] * 19 + [-1]
    cases["sparse_20"] = {
        "coefficients": coeffs,
        "true_roots": None,  # unit roots; checked structurally in tests
        "reference_roots": solve_case(coeffs),
    }

    out = {"generator": "mpmath.polyroots", "dps": mp.dps, "cases": {}}
    for name, case in cases.items():
        out["cases"][name] = {
            "coefficients": case["coefficients"],
            "true_roots": case["true_roots"],
            "reference_source": case.get("reference_source", "mpmath_polyroots"),
            "reference_roots": [
                {"re": str(z.real), "im": str(z.imag)} for z in case["reference_roots"]
            ],
        }

    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()
