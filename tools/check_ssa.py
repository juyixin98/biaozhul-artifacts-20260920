#!/usr/bin/env python3
"""Acceptance check: every SSA value has exactly one static definition.

Walks each SSA function, collects every value name defined by a
parameter, a phi result, or an instruction result, and fails if any name
is defined twice. Also runs the full :func:`verify_ssa` checks
(dominance + phi consistency) and interprets the raw and flat IR over a
few inputs as an end-to-end oracle.

Usage:
    python3 tools/check_ssa.py [example.mini inputs...]
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ssa_toolchain.analysis.verify import verify_ssa  # noqa: E402
from ssa_toolchain.interp import Interpreter  # noqa: E402
from ssa_toolchain.pipeline import run_pipeline  # noqa: E402


def count_definitions(f) -> dict[str, int]:
    counts: dict[str, int] = {}

    def note(name: str):
        counts[name] = counts.get(name, 0) + 1

    for p in f.params:
        note(p)
    for label in f.ordered_labels:
        b = f.blocks[label]
        for phi in b.phis:
            note(phi.dest)
        for ins in b.instrs:
            if ins.dest is not None:
                note(ins.dest)
    return counts


def main() -> int:
    cases = [
        ("examples/diamond.mini", [5]),
        ("examples/sum_loop.mini", [10]),
        ("examples/unreachable.mini", [0]),
        ("examples/swap_loop.mini", [4]),
        ("examples/nested.mini", [7]),
    ]
    if len(sys.argv) > 1:
        cases = [(sys.argv[1], [int(x) for x in sys.argv[2:]])]

    ok = True
    for path, inputs in cases:
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        res = run_pipeline(src, file=path, inputs=inputs)
        for name, fp in res.funcs.items():
            counts = count_definitions(fp.ssa)
            dupes = sorted(n for n, c in counts.items() if c != 1)
            uses = 0
            for label in fp.ssa.ordered_labels:
                b = fp.ssa.blocks[label]
                for ins in b.instrs:
                    uses += len(ins.operands)
                if b.term:
                    uses += len(b.term.operands)
            print(f"{os.path.basename(path)} :: {name}: "
                  f"{len(counts)} distinct SSA values, {uses} operand uses, "
                  f"{len(fp.removed)} pruned blocks")
            if dupes:
                print(f"  DUPLICATE DEFINITIONS: {dupes}")
                ok = False
            else:
                print("  single static definition: PASS")
            verify_ssa(fp.ssa)
            print("  verifier (dominance + phi consistency): PASS")
        ex = res.executions["main"]
        if not ex["agree"]:
            print("  execution agreement: FAIL")
            ok = False
        else:
            print(f"  raw/SSA/flat agree on {inputs}: "
                  f"return={ex['flat']['return']} PASS")
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
