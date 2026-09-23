"""Shared exhaustive differential-testing engine (the acceptance harness).

For every program case we:

1. build the Cartesian product of all declared input ranges;
2. run the **concrete reference interpreter** on each input, recording which
   faults actually occur (``div_by_zero`` / ``index_out_of_bounds``);
3. run the **interval analysis once** with the same input ranges and read its
   alarms;
4. assert **soundness**: every concrete fault is covered by a static alarm at
   the same source line (no false negatives);
5. assert the declared precision level:

   * ``exact``      — every alarm corresponds to an observed concrete fault
                      (no false positives either);
   * ``imprecise``  — extra alarms are allowed and the named expected false
                      positives must be present (this documents the
                      conservativity boundary).

Alarm keys are ``(kind, line)``.  Concrete execution keeps going to the first
fault per run, exactly like a real execution that aborts.
"""

import itertools

from intervalai import analyze_source
from intervalai.errors import (ConcreteExecError, IvalError, LexError,
                               ParseError, SemanticError)
from intervalai.pipeline import parse_program
from intervalai import concrete


class MalformedProgram(Exception):
    """A generated/supplied program fails lexing/parsing/resolution."""


def concrete_faults(prog, inputs, step_limit=20000):
    """Collect every (kind, line) that can be the first fault of some
    error-free execution prefix for fixed inputs (see
    ``concrete.collect_prefix_faults``)."""
    return concrete.collect_prefix_faults(prog, inputs, step_limit)


def parse_or_raise(src):
    try:
        return parse_program(src)
    except (LexError, ParseError, SemanticError) as e:
        raise MalformedProgram(str(e))


def analyze_alarms(src, bounds):
    rep = analyze_source(src, bounds, include_points=False)
    return rep, {(a["kind"], a["loc"]["line"]) for a in rep["alarms"]}, rep["alarms"]


def run_case(case):
    """Execute one acceptance case; return a result dict.

    ``case`` keys:
      name, source, ranges {input: (lo,hi)}, precision,
      expected_false_alarms [(kind, line)]  (for imprecise cases)
    """
    src = case["source"]
    try:
        prog = parse_or_raise(src)
    except MalformedProgram as e:
        return {"name": case["name"], "ok": True, "malformed": True,
                "runs": 0, "observed_faults": [], "alarms": [],
                "missed": [], "false_alarms": [], "problems": [],
                "precision": case.get("precision", "?"),
                "ascending_iterations": 0, "narrow_rounds": 0,
                "exit_state": None, "notes": f"skipped malformed: {e}"}

    ranges = case["ranges"]
    names = sorted(ranges)
    values = [range(ranges[n][0], ranges[n][1] + 1) for n in names]

    observed = set()
    runs = 0
    for combo in itertools.product(*values):
        inputs = dict(zip(names, combo))
        for fault in concrete_faults(prog, inputs):
            observed.add(fault)
        runs += 1

    bounds = {n: tuple(ranges[n]) for n in names}
    rep, alarm_keys, alarms = analyze_alarms(src, bounds)

    missed = observed - alarm_keys          # soundness violations
    extra = alarm_keys - observed           # false positives

    result = {
        "name": case["name"],
        "runs": runs,
        "malformed": False,
        "observed_faults": sorted(observed),
        "alarms": sorted(alarm_keys),
        "missed": sorted(missed),
        "false_alarms": sorted(extra),
        "ascending_iterations": rep["ascending_iterations"],
        "narrow_rounds": rep["narrow_rounds"],
        "exit_state": rep["exit_state"],
        "precision": case["precision"],
        "notes": case.get("notes", ""),
        "ok": True,
        "problems": [],
    }

    if missed:
        result["ok"] = False
        result["problems"].append(
            f"UNSOUND: concrete faults missed by the analysis: {sorted(missed)}")

    if case["precision"] == "exact" and extra:
        result["ok"] = False
        result["problems"].append(
            f"precision='exact' but got false alarms: {sorted(extra)}")

    if case["precision"] == "imprecise":
        expected_fp = {tuple(x) for x in case.get("expected_false_alarms", [])}
        missing_fp = expected_fp - extra
        if missing_fp:
            result["ok"] = False
            result["problems"].append(
                "declared imprecise case did not exhibit expected false "
                f"alarms: {sorted(missing_fp)}")
        # Every extra alarm should be one we explicitly document.
        undocumented = extra - expected_fp
        if case.get("require_all_documented", True) and undocumented:
            result["ok"] = False
            result["problems"].append(
                f"undocumented false alarms: {sorted(undocumented)}")
    return result
