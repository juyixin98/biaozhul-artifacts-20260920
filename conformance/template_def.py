"""Validation and compilation of process-template definitions.

A template definition is a JSON document describing a directed acyclic
process graph:

{
  "activities": ["register", "review", "approve", "reject", "notify"],
  "dependencies": [
    {"from": "register", "to": "review"},                 # mandatory edge
    {"any_of": ["approve", "reject"], "to": "notify"}     # inclusive join
  ],
  "exclusive_groups": [["approve", "reject"]],            # XOR branches
  "time_limits": [
    {"from": "register", "to": "review", "max_seconds": 172800},
    {"from": null, "to": "notify", "max_seconds": 604800}  # from case start
  ]
}

`validate_definition` normalizes this into a canonical form that is stored
on the published TemplateVersion; `CompiledTemplate` is the runtime view
used by the replay engine.
"""

from django.core.exceptions import ValidationError


def _fail(errors):
    raise ValidationError(errors)


def validate_definition(defn):
    """Validate a raw template definition, returning its normalized form.

    Raises ValidationError with a list of every problem found.
    """
    errors = []
    if not isinstance(defn, dict):
        _fail(["definition must be a JSON object"])

    activities = defn.get("activities")
    if (
        not isinstance(activities, list)
        or not activities
        or not all(isinstance(a, str) and a.strip() for a in activities)
    ):
        errors.append("activities must be a non-empty list of non-empty strings")
        activities = []
    elif len(set(activities)) != len(activities):
        errors.append("activities must be unique")
    actset = set(activities)

    # --- dependencies -----------------------------------------------------
    norm_deps = []
    raw_deps = defn.get("dependencies", [])
    if not isinstance(raw_deps, list):
        errors.append("dependencies must be a list")
        raw_deps = []
    for i, dep in enumerate(raw_deps):
        where = f"dependencies[{i}]"
        if not isinstance(dep, dict):
            errors.append(f"{where}: must be an object")
            continue
        to = dep.get("to")
        if not isinstance(to, str) or to not in actset:
            errors.append(f"{where}: 'to' must be a defined activity")
            continue
        if "any_of" in dep:
            srcs = dep.get("any_of")
            mode = "any"
            if (
                not isinstance(srcs, list)
                or not srcs
                or not all(isinstance(s, str) for s in srcs)
            ):
                errors.append(f"{where}: 'any_of' must be a non-empty list of activities")
                continue
        elif "from" in dep:
            src = dep.get("from")
            # an already-normalized "any" join keeps its mode on re-validation
            mode = "any" if dep.get("mode") == "any" else "all"
            # accept a single name or (idempotently) an already-normalized list
            if isinstance(src, str):
                srcs = [src]
            elif isinstance(src, list) and src and all(
                isinstance(s, str) for s in src
            ):
                srcs = src
            else:
                errors.append(f"{where}: 'from' must be an activity name")
                continue
        else:
            errors.append(f"{where}: needs either 'from' or 'any_of'")
            continue
        unknown = [s for s in srcs if s not in actset]
        if unknown:
            errors.append(f"{where}: unknown activities {unknown}")
            continue
        if to in srcs:
            errors.append(f"{where}: activity '{to}' cannot depend on itself")
            continue
        norm_deps.append({"from": sorted(set(srcs)), "to": to, "mode": mode})

    # --- exclusive groups ---------------------------------------------------
    norm_groups = []
    raw_groups = defn.get("exclusive_groups", [])
    if not isinstance(raw_groups, list):
        errors.append("exclusive_groups must be a list")
        raw_groups = []
    for i, group in enumerate(raw_groups):
        where = f"exclusive_groups[{i}]"
        if (
            not isinstance(group, list)
            or len(group) < 2
            or not all(isinstance(a, str) for a in group)
        ):
            errors.append(f"{where}: must be a list of at least two activity names")
            continue
        unknown = [a for a in group if a not in actset]
        if unknown:
            errors.append(f"{where}: unknown activities {unknown}")
            continue
        if len(set(group)) != len(group):
            errors.append(f"{where}: duplicate activities in group")
            continue
        norm_groups.append(sorted(group))

    # --- time limits --------------------------------------------------------
    norm_limits = []
    raw_limits = defn.get("time_limits", [])
    if not isinstance(raw_limits, list):
        errors.append("time_limits must be a list")
        raw_limits = []
    for i, tl in enumerate(raw_limits):
        where = f"time_limits[{i}]"
        if not isinstance(tl, dict):
            errors.append(f"{where}: must be an object")
            continue
        src = tl.get("from")  # null means "from case start"
        to = tl.get("to")
        max_seconds = tl.get("max_seconds")
        ok = True
        if src is not None and (not isinstance(src, str) or src not in actset):
            errors.append(f"{where}: 'from' must be null or a defined activity")
            ok = False
        if not isinstance(to, str) or to not in actset:
            errors.append(f"{where}: 'to' must be a defined activity")
            ok = False
        if (
            not isinstance(max_seconds, (int, float))
            or isinstance(max_seconds, bool)
            or max_seconds <= 0
        ):
            errors.append(f"{where}: 'max_seconds' must be a positive number")
            ok = False
        if ok and src is not None and src == to:
            errors.append(f"{where}: 'from' and 'to' must differ")
            ok = False
        if ok:
            norm_limits.append(
                {"from": src, "to": to, "max_seconds": int(max_seconds)}
            )

    # --- acyclicity (Kahn's algorithm over all dependency edges) ------------
    if actset and not errors:
        edges = [(s, d["to"]) for d in norm_deps for s in d["from"]]
        indegree = {a: 0 for a in actset}
        for _, t in edges:
            indegree[t] += 1
        queue = [a for a, deg in indegree.items() if deg == 0]
        visited = 0
        adjacency = {}
        for s, t in edges:
            adjacency.setdefault(s, []).append(t)
        while queue:
            node = queue.pop()
            visited += 1
            for nxt in adjacency.get(node, []):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    queue.append(nxt)
        if visited != len(actset):
            remaining = sorted(a for a, deg in indegree.items() if deg > 0)
            errors.append(
                f"dependency graph must be acyclic; cycle involves {remaining}"
            )

    if errors:
        _fail(errors)

    return {
        "activities": list(activities),
        "dependencies": norm_deps,
        "exclusive_groups": norm_groups,
        "time_limits": norm_limits,
    }


class CompiledTemplate:
    """Runtime view of a normalized template definition."""

    def __init__(self, definition):
        self.activities = set(definition["activities"])
        self.deps_to = {}
        for dep in definition["dependencies"]:
            self.deps_to.setdefault(dep["to"], []).append(dep)
        self.groups_of = {}
        for group in definition["exclusive_groups"]:
            for act in group:
                self.groups_of.setdefault(act, []).append(group)
        self.limits_to = {}
        for tl in definition["time_limits"]:
            self.limits_to.setdefault(tl["to"], []).append(tl)
        sources = {s for dep in definition["dependencies"] for s in dep["from"]}
        self.terminals = self.activities - sources
