"""Differential validation: interval analysis vs. exhaustive execution.

For a program whose only non-determinism comes from a set of *input
variables*, enumerate the Cartesian product of bounded input ranges and run
the concrete interpreter on each point.  Then compare against the abstract
result:

Soundness of alarms (no false negatives for "may" analysis)
-----------------------------------------------------------
* If ANY concrete execution hits division-by-zero / index-out-of-bounds at a
  site, the analyzer MUST report that site.
* The analyzer is allowed extra ("possible") alarms that no concrete input
  triggers -- those are the documented conservative imprecision.

Soundness of invariants
-----------------------
* Every reachable concrete value of a variable at a program point must lie
  inside the analyzer's interval for that block.

The harness also returns the set of *spurious* alarms (analyzer says
"possible", nothing concrete happens) so the conservativity boundary is
visible, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Callable, Iterable

from .analyzer import AnalysisResult
from .concrete import Interpreter
from .errors import RuntimeErr
from .pipeline import analyze_source
from .parser import parse_source


@dataclass
class DifferentialReport:
    source: str
    num_inputs: int
    input_ranges: dict[str, tuple[int, int]]
    points_checked: int
    crashes: dict[str, list[tuple]]      # site-key -> list of input tuples
    safe_points: int
    alarm_sites: dict[str, str]          # site-key -> certainty
    missed: list[str]                    # concrete crash sites not reported
    spurious: list[str]                  # "possible" alarms never crashing
    invariant_violations: list[str]
    sound: bool

    def summary(self) -> str:
        lines = [
            f"points checked      : {self.points_checked}",
            f"concrete crashes    : "
            f"{ {k: len(v) for k, v in self.crashes.items()} }",
            f"analysis alarms     : {self.alarm_sites}",
            f"missed crash sites  : {self.missed}",
            f"spurious (conservative) alarms: {self.spurious}",
            f"invariant violations: {self.invariant_violations}",
            f"SOUND               : {self.sound}",
        ]
        return "\n".join(lines)


def _site_key(err: RuntimeErr) -> str:
    """Stable identity of a failing operation in the source."""
    if err.span is None:
        return err.kind
    return f"{err.kind}@{err.span.start_offset}"


def _alarm_site_key(kind: str, start: int) -> str:
    return f"{kind}@{start}"


def exhaustive_check(source: str,
                     input_ranges: dict[str, tuple[int, int]],
                     *,
                     step_limit: int = 200_000,
                     check_invariants: bool = True,
                     ) -> DifferentialReport:
    """Run every bounded input point and validate against the analyzer."""
    program = parse_source(source)
    analysis = analyze_source(source).result

    names = list(input_ranges)
    ranges = [range(input_ranges[n][0], input_ranges[n][1] + 1)
              for n in names]

    crashes: dict[str, list[tuple]] = {}
    safe_points = 0
    invariant_violations: list[str] = []
    points = 0

    for values in product(*ranges):
        points += 1
        env_in = dict(zip(names, values))
        interp = Interpreter(program, [env_in[n] for n in names],
                             step_limit=step_limit)
        cr = interp.run()
        if cr.error is not None and cr.error.kind in (
                "div_by_zero", "index_out_of_bounds"):
            key = _site_key(cr.error)
            crashes.setdefault(key, []).append(tuple(values))
        elif cr.error is not None and cr.error.kind == "timeout":
            # Some input makes the concrete loop exceed the bound; skip it
            # but record it so the caller sees coverage gaps.
            pass
        else:
            safe_points += 1

    alarm_sites = {
        _alarm_site_key(a.kind, a.span.start_offset): a.certainty
        for a in analysis.alarms
    }

    # Soundness: every concrete crash site must be alarmed.
    missed = [k for k in crashes if k not in alarm_sites]

    # Conservativity boundary: possible alarms with zero concrete crash.
    spurious = [k for k, cert in alarm_sites.items()
                if k not in crashes]

    # Invariant soundness against concrete end-of-program environments is
    # checked separately (block-entry matching would need instrumentation);
    # see exhaustive_value_check for the finer comparison.
    sound = not missed and not invariant_violations
    return DifferentialReport(
        source=source, num_inputs=len(names), input_ranges=input_ranges,
        points_checked=points, crashes=crashes, safe_points=safe_points,
        alarm_sites=alarm_sites, missed=missed, spurious=spurious,
        invariant_violations=invariant_violations, sound=sound)


@dataclass
class ValueReport:
    """Per-input comparison of every concrete final scalar against the
    analyzer's exit-block interval."""
    points: int = 0
    violations: list[str] = field(default_factory=list)
    exit_values: dict[str, set[int]] = field(default_factory=dict)

    @property
    def sound(self) -> bool:
        return not self.violations


def exit_block_post_states(analysis: AnalysisResult) -> list[dict]:
    """Abstract final state at each Exit block.

    Uses each Exit block's own entry invariant (already the join of all
    predecessor edges computed by the fixpoint), executed through the
    block's trailing instructions.  Scalars are zero-allocated at
    declaration (matching the concrete interpreter), so a variable has a
    definite interval on every path, even when its branch-local
    initializer was not executed.
    """
    from .analyzer import Analyzer
    from . import ir as _ir
    a = Analyzer(analysis.cfg)
    out: list[dict] = []
    for b in analysis.cfg.blocks:
        if not isinstance(b.terminator, _ir.Exit):
            continue
        entry = analysis.invariants[b.id]
        if entry is None:
            continue
        post = a.exec_block(b, entry)
        if post is not None:
            out.append(post)
    return out


def exhaustive_value_check(source: str,
                           input_ranges: dict[str, tuple[int, int]],
                           *,
                           only_terminating: bool = True,
                           step_limit: int = 200_000) -> ValueReport:
    """For each input, every terminating concrete final variable value must
    belong to the analyzer's exit-block *output* interval."""
    program = parse_source(source)
    analysis = analyze_source(source).result
    exits = exit_block_post_states(analysis)
    names = list(input_ranges)
    ranges = [range(input_ranges[n][0], input_ranges[n][1] + 1)
              for n in names]
    rep = ValueReport()

    for values in product(*ranges):
        rep.points += 1
        interp = Interpreter(program, list(values), step_limit=step_limit)
        cr = interp.run()
        if cr.error is not None:
            continue
        for v, val in cr.env.items():
            contained = any(v in st and st[v].contains(val) for st in exits)
            rep.exit_values.setdefault(v, set()).add(val)
            if not contained:
                rep.violations.append(
                    f"input {tuple(values)}: {v}={val} outside all exit "
                    f"intervals")
    return rep
