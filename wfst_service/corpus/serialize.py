"""JSON-serialisable FST representation used by the SQLite index."""

from __future__ import annotations

from typing import Any

from ..core.fst import Arc, Fst
from ..corpus.symbols import EPS, EPS_LITERAL


def _label_out(label: str) -> str:
    return EPS_LITERAL if label == EPS else label


def fst_to_dict(fst: Fst) -> dict[str, Any]:
    return {
        "name": fst.name,
        "start": fst.start,
        "finals": [
            {"state": state, "cost": cost}
            for state, cost in sorted(fst.finals.items())
        ],
        "arcs": [
            {
                "src": arc.src,
                "dst": arc.dst,
                "in": _label_out(arc.ilabel),
                "out": _label_out(arc.olabel),
                "cost": arc.cost,
            }
            for arc in fst.arcs
        ],
    }


def fst_from_dict(data: dict[str, Any]) -> Fst:
    from ..corpus.spec import build_explicit_fst, TransducerSpec

    spec = TransducerSpec(name=data["name"], kind="fst", raw=data)
    return build_explicit_fst(spec)
