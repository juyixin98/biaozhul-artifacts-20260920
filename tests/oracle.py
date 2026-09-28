"""Independent test-side oracle.

CRITICAL: this module deliberately imports NOTHING from ``app``. It is a
from-scratch re-implementation of the documented timeline semantics,
written only against the problem/plan JSON shape, so it can disagree
with the production engine. Tests use it for two things:

1. independent validation of a concrete plan (categorized violations + a
   full per-phase timeline the production evidence is diffed against);
2. an independent optimal-makespan search with a *chronological*
   enumeration structure (group placements by start time), which differs
   from both production algorithms (flat multiset combinations and
   incremental DFS).

Category strings duplicate the public contract on purpose; if they drift
from ``app.rules.errors.FailureCategory`` the differential tests fail.
"""
from __future__ import annotations

import copy
from typing import Any

# Mirrors app.rules.errors.FailureCategory values (public string contract).
CAT_SCHEDULE_INVALID = "SCHEDULE_INVALID"
CAT_PRECONDITION = "PRECONDITION_VIOLATION"
CAT_INVARIANT = "INVARIANT_VIOLATION"
CAT_GOAL = "GOAL_NOT_REACHED"
CAT_RESOURCE = "RESOURCE_CONFLICT"
CAT_SIMULTANEOUS = "SIMULTANEOUS_CONFLICT"

CMP = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">=": lambda a, b: a >= b,
    "<=": lambda a, b: a <= b,
    ">": lambda a, b: a > b,
    "<": lambda a, b: a < b,
}


def _cond(condition: Any, state: dict[str, Any]) -> bool:
    if condition is None:
        return True
    assert isinstance(condition, dict) and len(condition) == 1
    (form, body), = condition.items()
    if form == "always":
        return body is True
    if form == "all":
        return all(_cond(c, state) for c in body)
    if form == "any":
        return any(_cond(c, state) for c in body)
    if form == "not":
        return not _cond(body, state)
    if form == "fact":
        op = body.get("op", "==")
        return CMP[op](state.get(body["fact"], 0), body["value"])
    raise AssertionError(f"oracle: unknown condition form {form!r}")


def _apply(effects: list[dict[str, Any]] | None, state: dict[str, Any]) -> dict[str, Any]:
    nxt = dict(state)
    for eff in effects or []:
        fact, value, op = eff["fact"], eff["value"], eff.get("op", "=")
        if op == "=":
            nxt[fact] = value
        elif op == "+=":
            nxt[fact] = nxt.get(fact, 0) + value
        elif op == "-=":
            nxt[fact] = nxt.get(fact, 0) - value
        else:
            raise AssertionError(f"oracle: unknown effect op {op!r}")
    return nxt


def _v(category: str, time: int, message: str, **extra: Any) -> dict[str, Any]:
    return {"category": category, "time": time, "message": message, **extra}


def validate_plan(problem: dict[str, Any], plan: dict[str, Any]) -> dict[str, Any]:
    """Independently replay a plan. Returns violations and phase evidence."""
    horizon = problem["horizon"]
    actions = {a["name"]: a for a in problem["actions"]}
    decl_index = {a["name"]: i for i, a in enumerate(problem["actions"])}
    violations: list[dict[str, Any]] = []

    bound: list[dict[str, Any]] = []
    for pos, step in enumerate(plan.get("steps", [])):
        action = actions.get(step["action"])
        if action is None:
            violations.append(
                _v(CAT_SCHEDULE_INVALID, step["start"], f"unknown action {step['action']!r}", action=step["action"])
            )
            continue
        dmin = action["duration_min"]
        dmax = action.get("duration_max", dmin)
        if not dmin <= step["duration"] <= dmax:
            violations.append(
                _v(CAT_SCHEDULE_INVALID, step["start"], "duration out of declared range", action=step["action"])
            )
        if step["start"] + step["duration"] > horizon:
            violations.append(
                _v(CAT_SCHEDULE_INVALID, step["start"] + step["duration"], "ends beyond horizon",
                   action=step["action"])
            )
        bound.append(step)

    # Half-open [start, end) resource occupations.
    for i, s1 in enumerate(bound):
        a1 = actions[s1["action"]]
        for s2 in bound[i + 1 :]:
            a2 = actions[s2["action"]]
            shared = set(a1.get("resources", [])) & set(a2.get("resources", []))
            d1, d2 = s1["duration"], s2["duration"]
            if shared and d1 > 0 and d2 > 0 and max(s1["start"], s2["start"]) < min(
                s1["start"] + d1, s2["start"] + d2
            ):
                point = max(s1["start"], s2["start"])
                for resource in sorted(shared):
                    violations.append(
                        _v(
                            CAT_RESOURCE,
                            point,
                            f"overlap on {resource!r}: [{s1['start']},{s1['start'] + d1}) vs "
                            f"[{s2['start']},{s2['start'] + d2})",
                            action=s1["action"],
                            resource=resource,
                        )
                    )

    # Same-instant write/write rejection (positive durations write at end, zero at start).
    def write_time(step: dict[str, Any]) -> int:
        return step["start"] if step["duration"] == 0 else step["start"] + step["duration"]

    writers: dict[tuple[int, str], list[str]] = {}
    for step in bound:
        for eff in actions[step["action"]].get("effects") or []:
            writers.setdefault((write_time(step), eff["fact"]), []).append(step["action"])
    for (when, fact), owners_list in sorted(writers.items()):
        if len(owners_list) > 1:
            violations.append(
                _v(
                    CAT_SIMULTANEOUS,
                    when,
                    f"fact {fact!r} written at t={when} by {len(owners_list)} instance(s) "
                    f"of actions {sorted(set(owners_list))}",
                    action=sorted(set(owners_list))[0],
                    resource=fact,
                )
            )

    def sort_key(step: dict[str, Any]) -> tuple[int, str, int]:
        return (decl_index[step["action"]], step["action"], step["start"])

    timeline: list[dict[str, Any]] = []
    state = copy.deepcopy(problem.get("initial", {}))
    for t in range(0, horizon + 1):
        ending = sorted(
            [s for s in bound if s["duration"] > 0 and s["start"] + s["duration"] == t], key=sort_key
        )
        starting = sorted([s for s in bound if s["start"] == t], key=sort_key)

        for step in ending:  # phase A: END
            before = dict(state)
            state = _apply(actions[step["action"]].get("effects"), state)
            timeline.append({"time": t, "phase": "END", "action": step["action"],
                             "before": before, "after": dict(state)})

        for step in starting:  # phase B: PRE (uniform post-end state)
            ok = _cond(actions[step["action"]].get("precondition"), state)
            if not ok:
                violations.append(
                    _v(CAT_PRECONDITION, t, f"precondition failed for {step['action']!r}", action=step["action"])
                )
            timeline.append({"time": t, "phase": "PRE", "action": step["action"],
                             "before": dict(state), "after": dict(state), "ok": ok})

        for step in starting:  # phase C: ZERO
            if step["duration"] != 0:
                continue
            before = dict(state)
            state = _apply(actions[step["action"]].get("effects"), state)
            timeline.append({"time": t, "phase": "ZERO", "action": step["action"],
                             "before": before, "after": dict(state)})

        active = sorted(
            [s for s in bound if s["duration"] > 0 and s["start"] <= t < s["start"] + s["duration"]],
            key=sort_key,
        )
        for step in active:  # phase D: invariant on every interior point
            ok = _cond(actions[step["action"]].get("invariant"), state)
            if not ok:
                violations.append(
                    _v(
                        CAT_INVARIANT,
                        t,
                        f"invariant failed at interior t={t} for {step['action']!r}",
                        action=step["action"],
                    )
                )
            timeline.append({"time": t, "phase": "INV", "action": step["action"],
                             "before": dict(state), "after": dict(state), "ok": ok,
                             "active": [x["action"] for x in active]})

    goal_ok = _cond(problem["goal"], state)
    if not goal_ok:
        violations.append(_v(CAT_GOAL, horizon, "goal not satisfied at horizon"))
    timeline.append({"time": horizon, "phase": "GOAL", "action": None,
                     "before": dict(state), "after": dict(state), "ok": goal_ok})

    return {
        "valid": not violations,
        "goal_satisfied": goal_ok,
        "violations": violations,
        "timeline": timeline,
        "final_state": state,
    }


# ---- independent chronological optimal search -------------------------

def _catalog(problem: dict[str, Any], bound: int) -> list[dict[str, Any]]:
    placements: list[dict[str, Any]] = []
    for ai, action in enumerate(problem["actions"]):
        dmax = action.get("duration_max", action["duration_min"])
        for duration in range(action["duration_min"], dmax + 1):
            for start in range(0, bound - duration + 1):
                placements.append({"action": action["name"], "decl": ai, "start": start, "duration": duration})
    return placements


def optimal_plan(
    problem: dict[str, Any],
    *,
    max_steps: int,
    max_occurrences: int,
) -> dict[str, Any]:
    """Iterative-deepening search grouped by start time.

    Structure differs on purpose from both production algorithms:
    placements are bucketed by start time and each schedule is a sequence
    of non-empty multisets over strictly increasing start times.
    """
    evaluated = 0
    horizon = problem["horizon"]

    def as_plan(chosen: list[dict[str, Any]]) -> dict[str, Any]:
        return {"steps": [{"action": p["action"], "start": p["start"], "duration": p["duration"]}
                          for p in chosen]}

    for bound in range(0, horizon + 1):
        by_time: dict[int, list[dict[str, Any]]] = {}
        for placement in _catalog(problem, bound):
            by_time.setdefault(placement["start"], []).append(placement)
        times = sorted(by_time)
        found: list[dict[str, Any]] | None = None

        def multiset_picks(
            bucket: list[dict[str, Any]],
            pos: int,
            picked: list[dict[str, Any]],
            counts: dict[int, int],
            *,
            consume: bool,
        ) -> list[tuple[list[dict[str, Any]], dict[int, int]]]:
            """All non-empty multisets (non-decreasing bucket positions) under caps."""
            out: list[tuple[list[dict[str, Any]], dict[int, int]]] = []

            def extend(j: int) -> None:
                for k in range(j, len(bucket)):
                    placement = bucket[k]
                    if counts.get(placement["decl"], 0) >= max_occurrences:
                        continue
                    if len(picked) >= max_steps:
                        continue
                    picked.append(placement)
                    counts[placement["decl"]] = counts.get(placement["decl"], 0) + 1
                    out.append((list(picked), dict(counts)))
                    extend(k)
                    picked.pop()
                    counts[placement["decl"]] -= 1

            extend(pos)
            return out

        def recurse(time_idx: int, chosen: list[dict[str, Any]], counts: dict[int, int]) -> None:
            nonlocal evaluated, found
            evaluated += 1
            if chosen and validate_plan(problem, as_plan(chosen))["valid"]:
                found = [dict(p) for p in chosen]
                return
            if found is not None or len(chosen) >= max_steps:
                return
            for j in range(time_idx, len(times)):
                for pick, new_counts in multiset_picks(
                    by_time[times[j]], 0, [], dict(counts), consume=True
                ):
                    if len(chosen) + len(pick) > max_steps:
                        continue
                    recurse(j + 1, chosen + pick, new_counts)
                    if found is not None:
                        return

        if bound == 0 and validate_plan(problem, {"steps": []})["valid"]:
            return {"found": True, "makespan": 0, "plan": {"steps": []}, "evaluated": evaluated}
        recurse(0, [], {})
        if found is not None:
            makespan = max(p["start"] + p["duration"] for p in found)
            return {"found": True, "makespan": makespan, "plan": as_plan(found), "evaluated": evaluated}
    return {"found": False, "makespan": None, "plan": None, "evaluated": evaluated}
