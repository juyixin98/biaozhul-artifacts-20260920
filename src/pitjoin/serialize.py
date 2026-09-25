"""证据/帧的 JSON 序列化（时间统一输出 UTC ISO-8601）。"""
from __future__ import annotations

from typing import Any

from .engine import JoinedFrame, SelectionEvidence
from .times import to_iso


def evidence_to_dict(ev: SelectionEvidence) -> dict[str, Any]:
    return {
        "entity_id": ev.entity_id,
        "feature_name": ev.feature_name,
        "event_time": to_iso(ev.event_time),
        "reason": ev.reason,
        "selected": None
        if ev.selected_record_id is None
        else {
            "record_id": ev.selected_record_id,
            "version": ev.selected_version,
            "value": ev.selected_value,
            "effective_time": to_iso(ev.selected_effective_time),
            "ingest_time": to_iso(ev.selected_ingest_time),
        },
        "candidates_total": ev.candidates_total,
        "candidates_asof": ev.candidates_asof,
        "tie_resolved": ev.tie_resolved,
        "excluded": [
            {
                "record_id": e.record_id,
                "version": e.version,
                "value": e.value,
                "effective_time": to_iso(e.effective_time),
                "ingest_time": to_iso(e.ingest_time),
                "cause": e.cause,
            }
            for e in ev.excluded
        ],
        "explanation": ev.explain(),
    }


def frame_to_dict(frame: JoinedFrame) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    for i, entity in enumerate(frame.entities):
        rows.append(
            {
                "entity_id": entity,
                "event_time": to_iso(int(frame.event_times[i])),
                "label": None
                if frame.labels[i] != frame.labels[i]
                else float(frame.labels[i]),
                "features": {
                    name: (None if (raw := float(col[i])) != raw else raw)
                    for name, col in frame.values.items()
                },
            }
        )
    return {
        "feature_names": list(frame.feature_names),
        "rows": rows,
        "reason_counts": frame.summary(),
        "evidence": [evidence_to_dict(ev) for ev in frame.evidence],
    }
