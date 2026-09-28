"""Serialization of kernel outcomes for the HTTP layer.

Kept separate from the statistical contract so the kernel never imports
fastapi/pydantic v2 response machinery.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Any


def outcome_to_dict(outcome) -> dict[str, Any]:
    payload = asdict(outcome)
    payload["decision_summary"] = {
        "request_id": outcome.request_id,
        "status": outcome.status,
        "accepted": outcome.status in ("ok", "weak"),
        "why": _why(outcome),
    }
    return payload


def _why(outcome) -> list[str]:
    ident = outcome.identification
    why = [
        f"order condition: {ident.order_detail}",
        f"rank condition: {'holds' if ident.rank_condition else 'FAILS'} "
        f"(rank={ident.rank_value}, required={ident.rank_required})",
        (
            f"strength: Cragg-Donald={ident.cragg_donald_statistic:.4g}, "
            f"min conditional F="
            f"{min((f.effective_f_statistic for f in outcome.first_stage), default=float('nan')):.4g}"
        ),
    ]
    if outcome.overidentification.testable:
        why.append(
            f"over-id ({outcome.overidentification.test_name}): {outcome.overidentification.detail}"
        )
    else:
        why.append("over-id: untestable (exclusion restriction remains an assumption)")
    why.append(f"endogeneity: {outcome.endogeneity.verdict} (p={outcome.endogeneity.p_value:.4g})")
    why.extend(outcome.warnings)
    return why
