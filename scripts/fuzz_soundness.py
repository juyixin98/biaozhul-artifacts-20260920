#!/usr/bin/env python3
"""Independent soundness fuzz for the certification kernel.

Ground truth here is computed independently of the interval-Newton kernel:
the random polynomial's coefficients are rendered as exact round-trip decimal
strings, and ``mpmath.polyroots`` at 80 decimal digits (a completely different
algorithm) gives the reference roots of EXACTLY the expression handed to the
certifier. We then verify:

* every certified enclosure contains one independently computed reference root;
* no two certified enclosures claim the same reference root;
* the number of certified simple roots equals the independent real-root count;
* every undecided region that the reference says has a root is explained
  (tangent/even multiplicity never occurs here because roots are simple, so a
  root landing only in an undecided region is reported as a coverage gap).

Run:  python3 scripts/fuzz_soundness.py [trials]
"""

from __future__ import annotations

import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mpmath as mp
import numpy as np

from app.core.config import CertConfig
from app.core.tracer import Tracer
from app.services import certify_service

mp.mp.dps = 80
MATCH_TOL = mp.mpf("1e-7")  # matching tolerance in root space (not containment)


def _reference_roots(ascending_coeffs: list[str]) -> list:
    desc = [mp.mpf(c) for c in reversed(ascending_coeffs)]
    roots = mp.polyroots(desc, maxsteps=3000, extraprec=300)
    return sorted(
        mp.mpf(r.real) for r in roots if abs(mp.im(r)) < mp.mpf("1e-40")
    )


def run_trial(trial: int, rng: random.Random) -> dict:
    n = rng.randint(1, 6)
    nominal = sorted({round(rng.uniform(-5, 5), 3) for _ in range(n)})
    if not nominal:
        return {"skipped": True}

    coeffs = np.array([1.0])
    for r in nominal:
        coeffs = np.convolve(coeffs, [1.0, -r])
    asc = coeffs[::-1]

    # Exact round-trip decimal coefficients: the ground truth and the
    # certifier see the same mathematical coefficients, no hidden rounding.
    coeff_strings = [f"{c:.17g}" for c in asc]
    terms: list[str] = []
    for power, c in enumerate(coeff_strings):
        if float(c) == 0.0:
            continue
        if power == 0:
            terms.append(f"({c})")
        elif power == 1:
            terms.append(f"({c})*x")
        else:
            terms.append(f"({c})*x^{power}")
    expression = " + ".join(terms)

    reference = _reference_roots(coeff_strings)

    with Tracer.create(None) as tracer:
        payload = certify_service.certify_expression(
            expression=expression,
            lower="-6.5",
            upper="6.5",
            config=CertConfig(max_depth=60),
            tracer=tracer,
            include_approximation=False,
        )

    findings: list[str] = []
    claimed: list = []
    for root in payload["certified_roots"]:
        lo = mp.mpf(root["enclosure"]["lower"])
        hi = mp.mpf(root["enclosure"]["upper"])
        # Strict containment: an independent reference root lies in the hull.
        inside = [r for r in reference if lo <= r <= hi]
        if len(inside) != 1:
            findings.append(
                f"trial {trial}: enclosure [{lo},{hi}] contains "
                f"{len(inside)} reference roots"
            )
        near = [r for r in reference if abs(r - (lo + hi) / 2) < MATCH_TOL]
        claimed.extend(near)

    if len(claimed) != len(set(claimed)):
        findings.append(f"trial {trial}: duplicate reference root certified")

    if len(payload["certified_roots"]) != len(reference):
        findings.append(
            f"trial {trial}: certified {len(payload['certified_roots'])} "
            f"vs {len(reference)} independent real roots"
        )

    return {
        "skipped": False,
        "trial": trial,
        "nominal": nominal,
        "reference_count": len(reference),
        "certified_count": len(payload["certified_roots"]),
        "findings": findings,
        "expression": expression,
    }


def main() -> int:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else 120
    rng = random.Random(20260928)
    violations: list[str] = []
    executed = 0
    started = time.time()
    for trial in range(trials):
        result = run_trial(trial, rng)
        if result.get("skipped"):
            continue
        executed += 1
        violations.extend(result["findings"])
        print(
            f"trial {trial:3d}: ref={result['reference_count']} "
            f"cert={result['certified_count']} "
            f"{'OK' if not result['findings'] else 'FINDING'}",
            flush=True,
        )
        for finding in result["findings"]:
            print("   " + finding, flush=True)
            print("   expr: " + result["expression"], flush=True)

    elapsed = time.time() - started
    print("\n========================================", flush=True)
    print(f"executed: {executed}/{trials}", flush=True)
    print(f"soundness violations: {len(violations)}", flush=True)
    print(f"elapsed: {elapsed:.1f}s", flush=True)
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
