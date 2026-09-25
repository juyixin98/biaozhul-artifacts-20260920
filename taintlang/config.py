"""Analysis configuration.

The taint model is annotation-free: instead of source-level syntax the
analysis recognises *marker* call expressions, ``source()`` / ``sink(x)`` /
``sanitize(x)`` by default.  Every name (and the context depth ``k``) is
configurable so a service caller can adapt the toolchain to different
conventions without touching the parser.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ConfigError

BUILTIN_SOURCE = "source"
BUILTIN_SINK = "sink"
BUILTIN_SANITIZE = "sanitize"


@dataclass(frozen=True)
class Config:
    """Configuration for one analysis run.

    Attributes:
        sources:     call names that introduce a fresh taint origin.
        sinks:       call names whose argument reaching the sink is reported.
        sanitizers:  call names modelled as *strong* cleaners: their result
                     never carries any of the input taint.
        k:           call-string depth.  Contexts are the last k call-site
                     identifiers; k=0 merges every call to a function
                     (0-CFA style), k=None means unlimited (bounded instead
                     by max_call_sites).
        entry_points: function names from which analysis starts; the first
                     declared function is the default sole entry point.
        max_paths:   per-finding cap on emitted source->sink paths.
        max_path_len: cap on path length (nodes) while searching.
        max_trails:  per-taint-fact cap on remembered flow trails during
                     the fixpoint; facts pruned past this are still tainted
                     but lose precise paths (reported as truncated).
        max_call_sites: hard bound on distinct call contexts per function,
                     guaranteeing termination even with k=None.
    """

    sources: tuple[str, ...] = (BUILTIN_SOURCE,)
    sinks: tuple[str, ...] = (BUILTIN_SINK,)
    sanitizers: tuple[str, ...] = (BUILTIN_SANITIZE,)
    k: int | None = 2
    entry_points: tuple[str, ...] = ()
    max_paths: int = 8
    max_path_len: int = 160
    max_trails: int = 24
    max_call_sites: int = 256

    @property
    def builtins(self) -> frozenset[str]:
        return frozenset(self.sources) | frozenset(self.sinks) | frozenset(self.sanitizers)

    @staticmethod
    def from_dict(data: dict | None) -> "Config":
        data = dict(data or {})

        def names(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
            if key not in data or data[key] is None:
                return default
            raw = data[key]
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, (list, tuple)) or not all(isinstance(x, str) for x in raw):
                raise ConfigError(f"'{key}' must be a list of strings")
            return tuple(raw)

        sources = names("sources", (BUILTIN_SOURCE,))
        sinks = names("sinks", (BUILTIN_SINK,))
        sanitizers = names("sanitizers", (BUILTIN_SANITIZE,))
        entries = names("entry_points", ())

        k = data.get("k", 2)
        if k is not None and (not isinstance(k, int) or isinstance(k, bool) or k < 0):
            raise ConfigError("'k' must be a non-negative integer or null")
        for key, lo in (("max_paths", 1), ("max_path_len", 1),
                        ("max_trails", 1), ("max_call_sites", 1)):
            val = data.get(key)
            if val is not None and (not isinstance(val, int) or isinstance(val, bool) or val < lo):
                raise ConfigError(f"'{key}' must be an integer >= {lo}")

        overlap = (frozenset(sources) & frozenset(sinks)) \
            | (frozenset(sources) & frozenset(sanitizers)) \
            | (frozenset(sinks) & frozenset(sanitizers))
        if overlap:
            raise ConfigError(f"source/sink/sanitizer names must be disjoint: {sorted(overlap)}")

        return Config(
            sources=sources,
            sinks=sinks,
            sanitizers=sanitizers,
            k=k,
            entry_points=entries,
            max_paths=int(data.get("max_paths", 8)),
            max_path_len=int(data.get("max_path_len", 160)),
            max_trails=int(data.get("max_trails", 24)),
            max_call_sites=int(data.get("max_call_sites", 256)),
        )
