#!/usr/bin/env python3
"""Run the kernel + independent oracle over a local JSON/functional fixture.

Usage:
    PYTHONPATH=src python3 scripts/reason.py data/fixture_mutex_instance.json
    PYTHONPATH=src python3 scripts/reason.py --functional data/fixture_hierarchy.owlf

Prints the concrete state distinction, evidence and cross-check verdict to
stdout -- no server required. This is the smallest reviewable artefact.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from min_iowl.lang.parser import axiom_from_json, parse_axioms  # noqa: E402
from min_iowl.service.reasoning import ReasoningService  # noqa: E402


def load_axioms(path: Path, functional: bool):
    if functional:
        return [(f"a{i + 1:03d}", a) for i, a in enumerate(parse_axioms(path.read_text()))]
    data = json.loads(path.read_text())
    return [(f"a{i + 1:03d}", axiom_from_json(a, i)) for i, a in enumerate(data["axioms"])]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture", type=Path)
    ap.add_argument("--functional", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit the full evidence JSON")
    args = ap.parse_args()

    items = load_axioms(args.fixture, args.functional)
    svc = ReasoningService()
    program = svc.compile(items)
    report, groups, ms = svc.reason(program)
    _, cross = svc.oracle_check([a for _, a in items], report)

    if args.json:
        from min_iowl.service.explain import shape_result

        result = shape_result(
            ontology_id=args.fixture.stem,
            program=program,
            report=report,
            groups=groups,
            cross_check=cross,
            duration_ms=ms,
            oracle_skipped=None,
        )
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    print(f"fixture           : {args.fixture.name}")
    print(f"engine            : miniowl-kernel/1.0.0")
    print(f"rules / base facts: {len(program.rules)} / {len(program.facts)}")
    print(f"reasoning time    : {ms:.3f} ms")
    print("-" * 64)
    print(f"ontology inconsistent : {report.inconsistent}")
    print(f"unsatisfiable classes : {[u.cls for u in report.unsatisfiable]}")
    print("equivalence groups    :",
          [list(g.members) for g in groups if len(g.members) > 1])
    print("entailed super-classes:")
    for cls in sorted(program.declared_classes):
        supers = sorted(report.tbox_supers.get(cls, frozenset()))
        print(f"    {cls:16s} -> {supers}")
    print("individuals:")
    for r in report.individuals:
        flag = "  *** MUTEX CONFLICT ***" if r.conflict else ""
        print(f"    {r.individual:10s} types={sorted(r.types)}{flag}")
        if r.conflict:
            c = r.conflict
            print(f"        disjoint pair : {list(c.disjoint_pair)}")
            print(f"        axiom sources : {list(c.sources)}")
    print("-" * 64)
    print(f"independent oracle: labels_explored={cross.labels_explored} "
          f"agrees={cross.agree}")
    if cross.mismatches:
        for m in cross.mismatches:
            print("    MISMATCH:", m)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
