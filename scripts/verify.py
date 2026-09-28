#!/usr/bin/env python3
"""End-to-end verification: FFT kernel vs. independent references, with evidence.

For every fixture case this script
  1. runs the FFT kernel,
  2. runs the dense NumPy reference (independent construction),
  3. runs the 50-digit mpmath reference for small cases,
  4. records error metrics, memory scale, input digests and the verdict
     in an append-only JSONL evidence log tied to a run UUID.

Exit code 0 iff every case passes; failures are logged, never hidden.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fixtures.generators import all_standard_cases  # noqa: E402
from toeplitz_fft import ToeplitzConfig, matmat, matvec  # noqa: E402
from toeplitz_fft.evidence import (  # noqa: E402
    EvidenceLogger,
    array_digest,
    compute_error_metrics,
    memory_report,
    runtime_metadata,
)
from toeplitz_fft.reference import dense_matmat, dense_matvec, mpmath_matvec  # noqa: E402

MPMATH_MAX_SIZE = 8  # run the big-number reference only when m, n <= this


def run_case(case, config, logger) -> bool:
    is_batch = case.x.ndim == 2
    k = case.x.shape[1] if is_batch else 1
    if is_batch:
        actual, meta = matmat(case.c, case.r, case.x, mode=case.mode,
                              config=config)
        dense = dense_matmat(case.c, case.r, case.x)
    else:
        actual, meta = matvec(case.c, case.r, case.x, mode=case.mode,
                              config=config)
        dense = dense_matvec(case.c, case.r, case.x)

    metrics = compute_error_metrics(actual, dense, config.err_rtol, config.err_atol)
    mem = memory_report(meta["m"], meta["n"], meta["L"], k, meta["mode"], config)

    record = {
        "case": case.name,
        "tags": case.tags,
        "shapes": {"m": meta["m"], "n": meta["n"], "k": k, "L": meta["L"]},
        "mode": meta["mode"],
        "dtype": meta["dtype"],
        "kernel_digest": meta["kernel_digest"],
        "input_digests": {
            "c": array_digest(case.c),
            "r": array_digest(case.r),
            "x": array_digest(case.x),
        },
        "vs_dense": metrics.to_dict(),
        "memory": mem,
    }

    passed = metrics.passed

    # Independent expected answer from the fixture itself (impulse/tiny/hand).
    if case.expected is not None:
        m_fix = compute_error_metrics(actual, case.expected,
                                      config.err_rtol, config.err_atol)
        record["vs_fixture_expected"] = m_fix.to_dict()
        passed = passed and m_fix.passed

    # Arbitrary-precision cross-check for small cases.
    if not is_batch and meta["m"] <= MPMATH_MAX_SIZE and meta["n"] <= MPMATH_MAX_SIZE:
        ref = np.array([complex(v) for v in mpmath_matvec(case.c, case.r, case.x)],
                       dtype=np.complex128)
        m_mp = compute_error_metrics(actual, ref, config.err_rtol, config.err_atol)
        record["vs_mpmath_50dps"] = m_mp.to_dict()
        passed = passed and m_mp.passed

    record["passed"] = passed
    logger.log("case", **record)
    status = "PASS" if passed else "FAIL"
    print(f"[{status}] {case.name:<14} m={meta['m']} n={meta['n']} L={meta['L']} "
          f"mode={meta['mode']:<7} max_abs={metrics.max_abs_err:.3e} "
          f"rel_l2={metrics.rel_l2_err:.3e} fft_bytes={mem['fft_total_bytes']} "
          f"dense_bytes={mem['dense_matrix_bytes']}")
    return passed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", default="evidence/verify.jsonl",
                        help="path of the JSONL evidence log")
    parser.add_argument("--pad-pow2", action="store_true",
                        help="pad embedding length to a power of two")
    args = parser.parse_args()

    config = ToeplitzConfig(pad_to_power_of_two=args.pad_pow2)
    cases = all_standard_cases()
    print(f"verifying {len(cases)} cases (pad_pow2={config.pad_to_power_of_two})")

    with EvidenceLogger(args.log) as logger:
        logger.log("run_start", versions=runtime_metadata(),
                   config={"real_dtype": config.real_dtype,
                           "complex_dtype": config.complex_dtype,
                           "pad_to_power_of_two": config.pad_to_power_of_two,
                           "err_rtol": config.err_rtol,
                           "err_atol": config.err_atol},
                   n_cases=len(cases))
        print(f"run_id={logger.run_id}  log={args.log}")
        results = [run_case(case, config, logger) for case in cases]
        total, failed = len(results), sum(1 for p in results if not p)
        logger.log("summary", total=total, failed=failed, passed=failed == 0)

    print(f"summary: {total - failed}/{total} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
