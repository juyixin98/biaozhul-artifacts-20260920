"""Built-in source / sanitizer / sink / propagator model.

All semantic knowledge about "what is dangerous" lives here; the fixpoint
engine is policy-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class BuiltinSpec:
    kind: str  # "source" | "sanitizer" | "validator" | "propagator"
              # | "sink" | "pure"
    #: indices of arguments whose taint flows to the result; empty tuple =
    #: none; used only for kind == "propagator"
    propagate_args: tuple[int, ...] = ()


def _source() -> BuiltinSpec:
    return BuiltinSpec(kind="source")


def _sanitizer() -> BuiltinSpec:
    return BuiltinSpec(kind="sanitizer")


def _validator() -> BuiltinSpec:
    return BuiltinSpec(kind="validator")


def _prop(*args: int) -> BuiltinSpec:
    return BuiltinSpec(kind="propagator", propagate_args=tuple(args))


#: Default security policy.  Per-request extras (API) are added on top.
DEFAULT_POLICY: dict[str, BuiltinSpec] = {
    # sources: result is unconditionally tainted
    "input": _source(),
    "read_file": _source(),
    "get_env": _source(),
    "recv": _source(),
    # sanitizers: result is provably clean regardless of input
    "escape": _sanitizer(),
    "sanitize": _sanitizer(),
    "int_cast": _sanitizer(),
    "encode": _sanitizer(),
    # validators: result is a clean boolean; on the TRUE branch their root
    # arguments are assumed safe (trusted guard, see README 误报边界)
    "validate": _validator(),
    # taint propagators
    "concat": _prop(),          # every argument propagates
    "wrap": _prop(0),           # only the first argument propagates
    "echo": _prop(0),
    "str_cast": _prop(0),
    # sinks (arguments are checked; result itself is clean)
    "sink": BuiltinSpec(kind="sink"),
    "exec": BuiltinSpec(kind="sink"),
    "eval": BuiltinSpec(kind="sink"),
    "query": BuiltinSpec(kind="sink"),
    "send": BuiltinSpec(kind="sink"),
    # pure helpers, never dangerous
    "len": BuiltinSpec(kind="pure"),
}


@dataclass(frozen=True)
class PolicyExtension:
    sources: tuple[str, ...] = ()
    sanitizers: tuple[str, ...] = ()
    validators: tuple[str, ...] = ()
    sinks: tuple[str, ...] = ()
    #: name -> argument indices that propagate (empty = all args)
    propagators: dict[str, tuple[int, ...]] | None = None


def build_policy(extension: PolicyExtension | None = None) -> dict[str, BuiltinSpec]:
    policy: dict[str, BuiltinSpec] = dict(DEFAULT_POLICY)
    if extension is None:
        return policy
    for name in extension.sources:
        policy[name] = _source()
    for name in extension.sanitizers:
        policy[name] = _sanitizer()
    for name in extension.validators:
        policy[name] = _validator()
    for name in extension.sinks:
        policy[name] = BuiltinSpec(kind="sink")
    for name, indices in (extension.propagators or {}).items():
        policy[name] = BuiltinSpec(
            kind="propagator",
            propagate_args=tuple(indices) if indices else (),
        )
    return policy


def classify(policy: dict[str, BuiltinSpec], name: str) -> str | None:
    spec = policy.get(name)
    return spec.kind if spec else None
