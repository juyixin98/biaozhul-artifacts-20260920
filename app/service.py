"""Application service: orchestrates parsing, exact isolation and evidence.

This is the single entry point used by the HTTP interface and the CLI. It owns
request-scoped diagnostics (request id, redaction, accept/reject/undetermined
status) and the explicit treatment of the constant zero polynomial.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Any

from . import polynomial as P
from .errors import (
    BudgetExceeded,
    FailureCode,
    InvalidRequestError,
    RootIsolationError,
)
from .evidence import verify_isolation
from .kernel import RootCell, isolate_real_roots
from .numeric_io import parse_polynomial
from .settings import Settings

logger = logging.getLogger("root_isolation")


@dataclass
class ServiceResponse:
    request_id: str
    status: str                 # "ok" | "zero_polynomial" | "undetermined" | "rejected"
    degree: int | None
    is_zero_polynomial: bool
    roots: list[dict[str, Any]] = field(default_factory=list)
    multiplicities: list[dict[str, Any]] = field(default_factory=list)
    evidence: dict[str, Any] | None = None
    diagnostics: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] | None = None


def _parse_optional_interval(raw_lo: Any, raw_hi: Any) -> tuple[Fraction, Fraction] | None:
    from .numeric_io import parse_rational
    if raw_lo is None and raw_hi is None:
        return None
    if raw_lo is None or raw_hi is None:
        raise InvalidRequestError(
            "search interval needs both endpoints",
            code=FailureCode.INVALID_INTERVAL,
        )
    lo = parse_rational(raw_lo, index=-1)
    hi = parse_rational(raw_hi, index=-1)
    if not lo < hi:
        raise InvalidRequestError(
            "search interval must satisfy lo < hi",
            code=FailureCode.INVALID_INTERVAL,
            state={"ordering": "lo >= hi"},
        )
    return lo, hi


def _serialize_cell(cell: RootCell) -> dict[str, Any]:
    return {
        "lo": str(cell.lo),
        "hi": str(cell.hi),
        "exact": cell.exact,
        "multiplicity": cell.multiplicity,
        "proof": {
            "method": "sturm",
            "variations_left": cell.variations_left,
            "variations_right": cell.variations_right,
            "root_count_in_cell": cell.variations_left - cell.variations_right
            if not cell.exact else 1,
            "chain_length": cell.chain_length,
            "open_interval": not cell.exact,
            "convention": "V(lo) - V(hi) counts roots in (lo, hi]",
        },
    }


def isolate_coefficients(
    coefficients: list[Any],
    settings: Settings,
    *,
    target_width: str = "1/1000000",
    interval_lo: Any = None,
    interval_hi: Any = None,
    request_id: str | None = None,
    include_evidence: bool = True,
) -> ServiceResponse:
    """Run the full pipeline with explicit accept / reject / undetermined."""
    request_id = request_id or uuid.uuid4().hex[:12]

    def diag(**state: Any) -> dict[str, Any]:
        base = {"request_id": request_id}
        base.update(state)
        return base

    try:
        try:
            width = Fraction(target_width)
        except (ValueError, ZeroDivisionError) as exc:
            raise InvalidRequestError(
                f"target_width is not a valid rational: {target_width!r}",
                code=FailureCode.INVALID_PRECISION,
            ) from exc
        if width <= 0:
            raise InvalidRequestError(
                "target_width must be a positive rational",
                code=FailureCode.INVALID_PRECISION,
            )
        search_interval = _parse_optional_interval(interval_lo, interval_hi)

        poly = parse_polynomial(coefficients, settings.kernel)

        # --- constant zero polynomial: explicit, no ordinary root list -----
        if P.is_zero(poly):
            logger.info("request accepted as zero polynomial",
                        extra={"request_id": request_id})
            return ServiceResponse(
                request_id=request_id,
                status="zero_polynomial",
                degree=None,
                is_zero_polynomial=True,
                diagnostics=diag(
                    decision="accepted",
                    reason="all coefficients are zero; every point is a root, "
                           "so no finite isolating interval list exists",
                    coefficient_count=len(coefficients),
                ),
            )

        degree = P.degree(poly)
        if degree == 0:
            logger.info("request accepted as non-zero constant",
                        extra={"request_id": request_id})
            return ServiceResponse(
                request_id=request_id,
                status="ok",
                degree=0,
                is_zero_polynomial=False,
                roots=[],
                multiplicities=[],
                evidence=None,
                diagnostics=diag(
                    decision="accepted",
                    reason="non-zero constant has no roots",
                    degree=0,
                ),
            )

        result = isolate_real_roots(
            poly, settings.kernel,
            target_width=width,
            search_interval=search_interval,
        )

        factors_by_m = {
            sf.multiplicity: sf.factor for sf in result.decomposition.factors
        }
        # Radical (square-free, distinct-root part) for the independent
        # companion-eigenvalue witness: product of the square-free factors.
        radical = P.POLY_ONE
        for sf in result.decomposition.factors:
            radical = P.mul(radical, sf.factor)
        evidence_report = None
        if include_evidence:
            evidence_report = verify_isolation(
                poly, radical, factors_by_m, result.cells, width,
                settings.kernel.precision_dps,
                full_line=search_interval is None,
            )

        roots = [_serialize_cell(c) for c in result.cells]
        multiplicities = [
            {
                "multiplicity": fr.multiplicity,
                "factor_degree": fr.degree,
                "sturm_chain_length": fr.chain_length,
                "cauchy_integer_bound": fr.cauchy_bound,
                "distinct_real_roots": fr.isolated_roots,
            }
            for fr in result.factor_reports
        ]

        evidence_dict: dict[str, Any] | None = None
        if evidence_report is not None:
            evidence_dict = {
                "accepted": evidence_report.accepted,
                "inconclusive": evidence_report.inconclusive,
                "contradicted": evidence_report.contradicted,
                "summary": evidence_report.summary,
                "cross_check": {
                    "method": evidence_report.cross_check.method,
                    "distinct_real_roots": evidence_report.cross_check.distinct_real_roots,
                    "locations": list(evidence_report.cross_check.locations),
                    "reliable": evidence_report.cross_check.reliable,
                    "note": evidence_report.cross_check.note,
                },
                "cells": [
                    {
                        "index": ce.index,
                        "verdict": ce.verdict,
                        "reasons": list(ce.reasons),
                        "width": ce.width,
                        "width_ok": ce.width_ok,
                        "endpoint_lo": ce.enclosure_lo.__dict__,
                        "endpoint_hi": ce.enclosure_hi.__dict__,
                        "brentq_root": ce.brentq_root,
                        "numeric_roots": list(ce.numeric_roots),
                    }
                    for ce in evidence_report.cells
                ],
            }

        if evidence_report is None or evidence_report.accepted:
            status = "ok"
            decision = "accepted"
            decision_reason = "all cells independently verified"
        else:
            # The exact Sturm cells are still a valid mathematical result;
            # the status reflects that an INDEPENDENT witness could not fully
            # confirm them. Contradiction means witnesses disagree;
            # inconclusive means precision/reliability limits were reached.
            status = "undetermined"
            decision = "undetermined"
            if evidence_report.contradicted:
                decision_reason = (
                    "exact isolation finished but an independent witness "
                    "CONTRADICTS a cell; investigate before trusting the result"
                )
            else:
                decision_reason = (
                    "exact isolation finished but an independent witness could "
                    "not certify a cell within the precision/reliability budget"
                )
        logger.info("isolation complete: %s", decision,
                    extra={"request_id": request_id})
        return ServiceResponse(
            request_id=request_id,
            status=status,
            degree=degree,
            is_zero_polynomial=False,
            roots=roots,
            multiplicities=multiplicities,
            evidence=evidence_dict,
            diagnostics=diag(
                decision=decision,
                reason=decision_reason,
                distinct_real_roots=result.distinct_real_roots,
                real_roots_counted_with_multiplicity=(
                    result.real_roots_with_multiplicity),
                search_interval=[str(result.search_lo), str(result.search_hi)],
                target_width=str(width),
                bisections_used=result.bisections_used,
                bisection_limit=result.bisection_limit,
                square_free_factors=len(result.decomposition.factors),
                evidence_accepted=(
                    evidence_report.accepted if evidence_report else None),
                evidence_inconclusive=(
                    evidence_report.inconclusive if evidence_report else None),
                evidence_contradicted=(
                    evidence_report.contradicted if evidence_report else None),
            ),
        )

    except BudgetExceeded as exc:
        logger.warning("undetermined: budget exceeded %s", exc.code.value,
                       extra={"request_id": request_id})
        return ServiceResponse(
            request_id=request_id,
            status="undetermined",
            degree=None,
            is_zero_polynomial=False,
            diagnostics=diag(
                decision="undetermined",
                reason=exc.message,
                **{f"state_{k}": _safe_state(v) for k, v in exc.state.items()},
            ),
            failure={"code": exc.code.value, "message": exc.message,
                     "state": {k: _safe_state(v) for k, v in exc.state.items()}},
        )
    except RootIsolationError as exc:
        logger.warning("rejected: %s", exc.code.value,
                       extra={"request_id": request_id})
        return ServiceResponse(
            request_id=request_id,
            status="rejected",
            degree=None,
            is_zero_polynomial=False,
            diagnostics=diag(
                decision="rejected",
                reason=exc.message,
                **{f"state_{k}": _safe_state(v) for k, v in exc.state.items()},
            ),
            failure={"code": exc.code.value, "message": exc.message,
                     "state": {k: _safe_state(v) for k, v in exc.state.items()}},
        )


def _safe_state(value: Any) -> Any:
    """Redact anything that could carry user data; keep small scalar facts."""
    if isinstance(value, (int, float, bool)):
        return value
    if isinstance(value, str) and len(value) <= 80:
        # Strings produced internally are state labels, never coefficient data.
        return value
    return f"<{type(value).__name__}>"
