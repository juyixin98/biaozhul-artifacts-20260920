"""Direct library demo (no server needed):

    PYTHONPATH=src python3 examples/demo_core.py

Shows the three classifications, exact parametric substitution and the
contradiction witness, all computed with fractions.Fraction.
"""

from __future__ import annotations

from fractions import Fraction

from rational_linalg.evidence import (
    verify_contradiction_witness,
    verify_solution,
)
from rational_linalg.solve import solve_augmented


def show(title: str, A, b) -> None:
    print(f"\n=== {title} ===")
    result = solve_augmented(A, b)
    kind = result["classification"]
    print("classification:", kind, "| rank A:", result["rank_a"],
          "| rank [A|b]:", result["rank_augmented"])
    if kind == "inconsistent":
        evidence = verify_contradiction_witness(A, b, result["contradiction_witness"])
        print("witness y      :", [str(v) for v in result["contradiction_witness"]])
        print("y^T A (zeros)  :", [str(v) for v in evidence["yt_A"]])
        print("y^T b (nonzero):", evidence["yt_b"])
    else:
        print("particular x0  :", [str(v) for v in result["particular"]])
        for j, v in enumerate(result["null_basis"]):
            print(f"null direction {j}:", [str(x) for x in v])
        t = [Fraction(3, 2) for _ in result["null_basis"]]
        check = verify_solution(
            A, b, result["particular"], result["null_basis"], t
        )
        print("substitute t = 3/2 -> exact residual zero:", check["ok"])


if __name__ == "__main__":
    show(
        "unique, big common factor 10^20",
        [[Fraction(10**20), Fraction(2 * 10**20)],
         [Fraction(3 * 10**20), Fraction(5 * 10**20)]],
        [Fraction(5 * 10**20), Fraction(13 * 10**20)],
    )
    show(
        "infinitely many solutions",
        [[1, 2, 3], [4, 5, 6], [6, 9, 12]],
        [6, 15, 27],
    )
    show(
        "inconsistent",
        [[1, 1], [1, 1]],
        [1, 2],
    )
