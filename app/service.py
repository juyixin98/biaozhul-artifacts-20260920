"""Service orchestration: wires the statistical modules into one pipeline.

Pipeline order matters:
  1. build fixed-weight BALANCED panel by object identity (missing != zero);
  2. screen contamination (move bad counterfactuals to excluded_records);
  3. estimate the 2x2 DID with object-clustered SE;
  4. cross-check with the INDEPENDENT levels OLS path;
  5. run the pre-trend diagnostic (diagnoses, never proves).
"""
from __future__ import annotations

import platform
from typing import Any

from . import __version__
from .config import SETTINGS
from .contracts import (
    DIDRequest,
    DIDResponse,
    EventStudyRequest,
    EventStudyResponse,
    Failure,
    FailureCategory,
    Severity,
)
from .diagnostics import pretrend_diagnostic, screen_contamination
from .event import run_event_study
from .kernel import estimate_did
from .panel import build_balanced_panel
from .reference import levels_reference_regression

SERVICE_NAME = SETTINGS.service_name


def _processing_location() -> str:
    return f"{platform.node() or 'local'}:python-{platform.python_version()}"


def _has_error(failures: list[Failure]) -> bool:
    return any(f.severity is Severity.ERROR for f in failures)


def run_did(request: DIDRequest) -> DIDResponse:
    method_notes = [
        "Balanced sample: an object enters only if observed in BOTH periods; "
        "a missing period is never imputed as zero.",
        "Weights are fixed per object (weight 1.0 in both periods).",
        "Standard errors cluster by object (CR1 sandwich).",
        "Parallel trends is a diagnostic: rejection is evidence against, "
        "non-rejection is not proof.",
    ]

    panel = build_balanced_panel(
        request.observations, request.pre_period, request.post_period
    )
    contamination = screen_contamination(
        panel,
        request.allow_contaminated_controls,
        request.pre_period,
        request.post_period,
    )

    kernel_result = estimate_did(panel, request.pre_period, request.post_period)

    failures = list(panel.failures)
    reference = None
    if kernel_result is not None:
        failures = kernel_result.failures
        if request.include_reference_regression:
            reference = levels_reference_regression(
                list(panel.aligned.values()), kernel_result.decomposition.did
            )

    pretrend = pretrend_diagnostic(
        request.observations, request.earlier_period, request.pre_period
    )
    if pretrend.conclusion == "parallel_trends_rejected":
        failures.append(
            Failure(
                category=FailureCategory.PRETREND_REJECTED,
                severity=Severity.WARNING,
                message="Pre-trend diagnostic REJECTS parallel trends on the "
                "observed pre window; interpret the DID with caution. This "
                "diagnostic cannot prove parallel trends.",
            )
        )

    status = "refused" if _has_error(failures) else "ok"

    summary: dict[str, Any] = {
        "processing_location": _processing_location(),
        "n_supplied_observations": len(request.observations),
        "n_excluded_records": len(panel.excluded),
        "n_estimating_objects": (
            kernel_result.clustered_se.n_objects if kernel_result else 0
        ),
    }
    if kernel_result is not None:
        summary["did"] = kernel_result.decomposition.did

    return DIDResponse(
        request_id=request.request_id,
        service=SERVICE_NAME,
        version=__version__,
        status=status,
        decomposition=kernel_result.decomposition if kernel_result else None,
        clustered_se=kernel_result.clustered_se if kernel_result else None,
        reference_regression=reference,
        pretrend=pretrend,
        contamination=contamination,
        excluded_records=panel.excluded,
        failures=failures,
        method_notes=method_notes,
        summary=summary,
    )


def run_event(request: EventStudyRequest) -> EventStudyResponse:
    points, excluded, failures, summary = run_event_study(
        request.observations, request.min_event_time, request.max_event_time
    )
    status = "refused" if _has_error(failures) else "ok"
    method_notes = [
        "Supported model: single common treatment cohort + never-treated "
        "controls; event time -1 is normalized to 0.",
        "Staggered adoption is outside support and is explicitly REFUSED.",
        "Each point aligns an object only if observed in both the target and "
        "the normalization period; a missing period is never zero.",
    ]
    summary = dict(summary)
    summary["processing_location"] = _processing_location()

    return EventStudyResponse(
        request_id=request.request_id,
        service=SERVICE_NAME,
        version=__version__,
        status=status,
        points=points,
        excluded_records=excluded,
        failures=failures,
        method_notes=method_notes,
        summary=summary,
    )
