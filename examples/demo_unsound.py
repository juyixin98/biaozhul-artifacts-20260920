#!/usr/bin/env python3
"""Demonstrate why the value restriction is required for mutable references.

The classic counterexample (a variant of Wright/Felleisen-style unsoundness)::

    let r = ref (fun x -> x) in          (* a cell holding the identity *)
    r := (fun n -> n + 1);               (* overwrite with an int->int fn *)
    let f = !r in                        (* f : forall a. a -> a  (!!) *)
    let a = f true in                    (* use f at bool -> bool *)
    let b = f 0 in                       (* use f at int -> int *)
    b

With *naive* generalization at every let (no value restriction) the inferrer
accepts this program and even assigns ``f`` the polymorphic type
``forall a. a -> a``. At runtime the single cell contains ``n -> n + 1``, so
``f true`` applies integer addition to a boolean and crashes.

With the value restriction (the default), ``r`` is kept monomorphic at type
``(? -> ?) ref``; once ``r := fun n -> n + 1`` fixes that type, ``f`` is
monomorphic ``int -> int`` and ``f true`` is rejected with the location of
the conflicting expression.

Usage::

    python examples/demo_unsound.py            # runs both modes, exit 0
    python examples/demo_unsound.py --check    # intended for CI: asserts behavior
"""

from __future__ import annotations

import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from miniml.pipeline import compile_source, evaluate, CompileFailure  # noqa: E402
from miniml.errors import render_compile_error  # noqa: E402

SOURCE = """\
(* The classic "shared reference" unsoundness example (one expression). *)
let r = ref (fun x -> x) in
r := (fun n -> n + 1);
let f = !r in
let a = f true in
let b = f 0 in
b
"""


def run_sound() -> CompileFailure:
    print("=" * 72)
    print("MODE 1: value restriction ON (the sound default)")
    print("=" * 72)
    try:
        compile_source(SOURCE, value_restriction=True)
    except CompileFailure as f:
        print(f.rendered)
        print(f"=> rejected at the {f.phase} phase ({f.code})")
        return f
    raise SystemExit("UNEXPECTED: the unsound program was accepted with VR on")


def run_unsound():
    print()
    print("=" * 72)
    print("MODE 2: value restriction OFF (naive generalization, for contrast)")
    print("=" * 72)
    program, result = compile_source(SOURCE, value_restriction=False)
    from miniml.types import type_str
    print(f"final expression type: {type_str(result.expr_type)}")
    # Under naive generalization f receives forall a. a -> a: show the two
    # incompatible instantiations from the derivation trace.
    inst = [ev.detail for ev in result.trace
            if ev.step == "instantiate" and ev.detail.startswith("variable 'f'")]
    for line in inst:
        print("  ", line)
    print()
    print("f was generalized to forall a. a -> a and instantiated once at")
    print("bool -> bool and once at int -> int, even though it reads one cell.")
    print()
    try:
        evaluate(program)
    except CompileFailure as f:
        print(f.rendered)
        print(f"=> accepted by inference, but RUNTIME PANIC ({f.code}):")
        print(f"   {f.message}")
        return f
    raise SystemExit("UNEXPECTED: the evaluator did not crash (counterexample broken)")


def main() -> int:
    check = "--check" in sys.argv[1:]
    rejected = run_sound()
    panic = run_unsound()
    if check:
        assert rejected.code in ("E003", "E004"), rejected.code
        assert panic.code == "E005", panic.code
        print("\nCHECK OK: VR rejects the program; without VR inference accepts and evaluation panics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
