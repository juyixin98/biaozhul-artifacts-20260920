#!/usr/bin/env python3
"""Standalone verification script (evidence runner).

Runs the synthetic fixture suite through the engine and checks every result
against the independent oracles.  Emits:
  * a progress log on stderr (and ``--log-file``);
  * a JSON evidence report (``--report-file`` or stdout).

Exit status is explicit: 0 only when every case passes; 1 on any
numeric/assertion failure; 2 on setup error.  Nothing is reported as passed
without being executed.

Usage:
    python scripts/verify.py
    python scripts/verify.py --report-file reports/evidence.json \
        --log-file reports/verify.log
    TOEPLITZ_VERIFY_LARGE=1 python scripts/verify.py   # include large cases
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from toeplitz_fft.engine import Engine  # noqa: E402
from toeplitz_fft.evidence import verify_result  # noqa: E402
from toeplitz_fft.fixtures import (  # noqa: E402
    DEFAULT_SEED,
    NumpyFixture,
    expected_impulse_columns,
    fixture_to_payload,
    make_asymmetric_real,
    make_complex_non_hermitian,
    make_impulse_case,
    make_tiny_case,
)
from toeplitz_fft.inputs import parse_problem  # noqa: E402
from toeplitz_fft.kernels import (  # noqa: E402
    padded_embedding_size,
    memory_scale_report,
)
from toeplitz_fft.logging_ctx import (  # noqa: E402
    configure_logging,
    environment_versions,
    new_run_id,
    set_run_id,
)
from toeplitz_fft.service import serialize_output  # noqa: E402


def build_cases() -> list[tuple[NumpyFixture, dict[str, Any]]]:
    """(fixture, per-case expectations).  Independent of the FFT core."""
    cases: list[tuple[NumpyFixture, dict[str, Any]]] = []

    tiny = make_tiny_case(1)
    cases.append((tiny, {
        "mode": "real", "precision": "double",
        "expected_kernel_path": "tiny_explicit",
        "expected_result": [[7.0], [-14.0]],  # 3.5 * [2, -4], hand computed
        "rtol": 0.0, "atol": 0.0,
    }))

    cases.append((make_asymmetric_real(13, batch=4), {
        "mode": "real", "precision": "double",
        "expected_kernel_path": "embedding_fft",
    }))
    cases.append((make_complex_non_hermitian(17, batch=2), {
        "mode": "complex", "precision": "double",
        "expected_kernel_path": "embedding_fft",
    }))

    impulse_positions = (0, 7, 14)
    impulse = make_impulse_case(15, positions=impulse_positions)
    impulse_expected = expected_impulse_columns(
        impulse.first_column, impulse.first_row, impulse_positions)
    cases.append((impulse, {
        "mode": "real", "precision": "double",
        "expected_kernel_path": "embedding_fft",
        "expected_impulse_columns": impulse_expected,
    }))

    # Single precision real case on a non-power-of-two length.
    cases.append((make_asymmetric_real(31, seed=DEFAULT_SEED + 7, batch=2), {
        "mode": "real", "precision": "single",
        "expected_kernel_path": "embedding_fft",
    }))

    # Force the embedding FFT on a tiny n to show it is also correct there.
    cases.append((make_tiny_case(1), {
        "mode": "real", "precision": "double", "kernel": "embedding_fft",
        "expected_kernel_path": "embedding_fft",
        "expected_result": [[7.0], [-14.0]], "rtol": 0.0, "atol": 1e-12,
    }))

    if os.environ.get("TOEPLITZ_VERIFY_LARGE") == "1":
        # Memory-scale demonstration: n=4096, no dense matrix is formed.
        # Evidence: 64 deterministic high-precision mpmath element probes.
        cases.append((make_asymmetric_real(4096, seed=DEFAULT_SEED + 9,
                                           batch=2), {
            "mode": "real", "precision": "double",
            "expected_kernel_path": "embedding_fft",
            "sample_count": 64,
        }))
    return cases


def _close(actual: Any, expected: Any, *, rtol: float, atol: float) -> bool:
    import numpy as np
    a = np.asarray(actual, dtype=np.complex128)
    e = np.asarray(expected, dtype=np.complex128)
    return bool(np.allclose(a, e, rtol=rtol, atol=atol))


def run(run_id: str, log: logging.Logger) -> tuple[list[dict[str, Any]], bool]:
    engine = Engine()
    reports: list[dict[str, Any]] = []
    all_passed = True

    cases = build_cases()
    log.info("run=%s versions=%s", run_id, json.dumps(environment_versions()))
    log.info("run=%s cases_total=%d progress=0/%d", run_id, len(cases),
             len(cases))

    for idx, (fixture, expectation) in enumerate(cases, start=1):
        case_id = f"{run_id}:{fixture.case_id}"
        log.info("case=%s progress=%d/%d input n=%d batch=%d desc=%r",
                 case_id, idx, len(cases), fixture.n, fixture.batch,
                 fixture.description)
        payload = fixture_to_payload(
            fixture,
            mode=expectation["mode"],
            precision=expectation.get("precision", "double"),
            kernel=expectation.get("kernel", "auto"),
        )
        case_record: dict[str, Any] = {
            "case_id": case_id,
            "fixture": fixture.case_id,
            "n": fixture.n,
            "batch": fixture.batch,
            "description": fixture.description,
            "steps": [],
        }
        try:
            problem = parse_problem(payload)
            case_record["steps"].append("payload parsed and validated")
            result = engine.run(problem)
            case_record["steps"].append(
                f"engine path={result.plan.path} m={result.plan.m} "
                f"cache_hit={result.cache_hit}")

            assertions: list[dict[str, Any]] = []

            # 1) path assertion
            path_ok = result.plan.path == expectation["expected_kernel_path"]
            assertions.append({"name": "kernel_path", "expected":
                               expectation["expected_kernel_path"],
                               "actual": result.plan.path, "passed": path_ok})
            log.info("case=%s check=kernel_path passed=%s expected=%s actual=%s",
                     case_id, path_ok, expectation["expected_kernel_path"],
                     result.plan.path)

            # 2) concrete expected values (hand-computed / structural)
            if "expected_result" in expectation:
                ok = _close(serialize_output(result.output),
                            expectation["expected_result"],
                            rtol=expectation.get("rtol", 1e-9),
                            atol=expectation.get("atol", 1e-10))
                assertions.append({"name": "concrete_expected_result",
                                   "passed": ok})
                log.info("case=%s check=concrete_expected_result passed=%s",
                         case_id, ok)

            if "expected_impulse_columns" in expectation:
                ok = _close(result.output,
                            expectation["expected_impulse_columns"],
                            rtol=1e-12, atol=1e-10)
                assertions.append({"name": "impulse_is_column_of_T",
                                   "passed": ok})
                log.info("case=%s check=impulse_is_column_of_T passed=%s",
                         case_id, ok)

            # 3) independent oracle evidence
            import numpy as np
            sample_count = expectation.get("sample_count")
            sample_indices = None
            if sample_count is not None:
                from toeplitz_fft.evidence import deterministic_sample_indices
                sample_indices = deterministic_sample_indices(
                    fixture.n, fixture.batch, sample_count)
            report = verify_result(
                problem,
                result.output.astype(
                    np.complex128 if problem.is_complex else np.float64),
                cache_hit=result.cache_hit,
                kernel_path=result.plan.path,
                memory=result.stats.get("memory", {}),
                case_id=case_id,
                use_dense_oracle=expectation.get("use_dense_oracle", True),
                sample_indices=sample_indices,
            )
            evidence = report.to_dict()
            assertions.append({
                "name": "independent_oracles",
                "passed": report.passed,
                "against_mpmath_max_abs":
                    evidence["against_mpmath"]["max_abs_error"],
                "basis": evidence["against_mpmath"]["basis"],
            })
            log.info(
                "case=%s check=oracles passed=%s fft_max_abs_err=%.3e "
                "fft_max_rel_err=%.3e",
                case_id, report.passed,
                evidence["against_mpmath"]["max_abs_error"],
                evidence["against_mpmath"]["max_rel_error"])

            # 4) embedding no-aliasing size assertion
            m = result.plan.m
            no_alias = m >= 2 * fixture.n - 1
            assertions.append({"name": "embedding_m_ge_2n-1",
                               "expected": f">= {2*fixture.n-1}",
                               "actual": m, "passed": no_alias})
            log.info("case=%s check=no_aliasing passed=%s m=%d 2n-1=%d",
                     case_id, no_alias, m, 2 * fixture.n - 1)

            case_passed = all(a["passed"] for a in assertions)
            case_record.update({
                "passed": case_passed,
                "failure_category": None if case_passed else "numeric_accuracy",
                "plan": {"path": result.plan.path, "m": m,
                         "coefficient_digest": result.plan.coefficient_digest,
                         "kernel_digest": result.plan.kernel_digest},
                "assertions": assertions,
                "evidence": evidence,
            })
        except Exception as exc:  # explicit failure record, never silent
            log.exception("case=%s FAILED category=kernel_failed err=%s",
                          case_id, exc)
            case_record.update({
                "passed": False,
                "failure_category": "kernel_failed",
                "error": f"{exc.__class__.__name__}: {exc}",
            })
            case_passed = False

        # Second identical call demonstrates cache binding on the FFT path.
        # The tiny explicit path has nothing to cache by design; there we
        # only require bit-identical recomputation.
        if case_record.get("passed"):
            result2 = engine.run(parse_problem(payload))
            import numpy as np
            same = np.array_equal(result.output, result2.output)
            log.info("case=%s repeat_call path=%s cache_hit=%s same_result=%s",
                     case_id, result2.plan.path, result2.cache_hit, same)
            cache_ok = same and (
                result2.cache_hit if result2.plan.path == "embedding_fft"
                else True)
            case_record["repeat_cache_hit"] = result2.cache_hit
            case_record["repeat_result_identical"] = bool(same)
            if not cache_ok:
                case_record["passed"] = False
                case_record["failure_category"] = "cache_binding_failed"
                case_passed = False

        reports.append(case_record)
        all_passed = all_passed and case_record["passed"]
        log.info("case=%s verdict=%s progress=%d/%d cache_info=%s",
                 case_id, "PASS" if case_record["passed"] else "FAIL",
                 idx, len(cases), json.dumps(engine.cache_info()))

    log.info("run=%s memory_scale_sample=%s", run_id, json.dumps(
        memory_scale_report(4096, 2, padded_embedding_size(4096),
                            __import__("numpy").dtype("float64"))))
    return reports, all_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-file", type=Path, default=None)
    parser.add_argument("--log-file", type=Path, default=None)
    args = parser.parse_args(argv)

    log = configure_logging("INFO")
    if args.log_file:
        args.log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(args.log_file, mode="w", encoding="utf-8")
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s [run=%(run_id)s] %(message)s"))
        log.addHandler(fh)

    run_id = set_run_id(new_run_id("verify"))
    started = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    log.info("verification_start run=%s time=%s seed=%d", run_id, started,
             DEFAULT_SEED)

    try:
        reports, passed = run(run_id, log)
    except Exception as exc:
        log.exception("verification_aborted run=%s err=%s", run_id, exc)
        return 2

    summary = {
        "run_id": run_id,
        "started": started,
        "versions": environment_versions(),
        "seed": DEFAULT_SEED,
        "cases": len(reports),
        "passed": sum(1 for r in reports if r["passed"]),
        "failed": sum(1 for r in reports if not r["passed"]),
        "overall_passed": passed,
        "reports": reports,
    }
    rendered = json.dumps(summary, indent=2, default=str)
    if args.report_file:
        args.report_file.parent.mkdir(parents=True, exist_ok=True)
        args.report_file.write_text(rendered, encoding="utf-8")
        log.info("report_written path=%s overall_passed=%s",
                 args.report_file, passed)
    else:
        print(rendered)
    log.info("verification_end run=%s overall_passed=%s passed=%d failed=%d",
             run_id, passed, summary["passed"], summary["failed"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
