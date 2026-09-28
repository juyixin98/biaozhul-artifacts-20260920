"""Configuration loading and per-request overrides.

Defaults live in ``config/defaults.json`` so that the exact precision ladder
and tolerances used for an acceptance run are inspectable and reproducible.
A different file can be selected with the env var ``MIPSOLVER_CONFIG``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_CONFIG_PATH = _REPO_ROOT / "config" / "defaults.json"

# Keys a client is allowed to override per request. Overriding the ladder per
# request is what lets tests force a "precision exhausted" outcome.
_OVERRIDABLE_KEYS = frozenset(
    {
        "backward_tol",
        "forward_tol",
        "max_iterations_per_stage",
        "stagnation_factor",
        "use_fp32_first",
        "use_fp64",
        "mp_dps_ladder",
        "solution_output_dps",
        "redact_payload",
    }
)


@dataclass(frozen=True)
class Config:
    """Resolved solver configuration (immutable)."""

    residual_dps: int
    backward_tol: str
    forward_tol: str
    max_iterations_per_stage: int
    stagnation_factor: str
    use_fp32_first: bool
    use_fp64: bool
    mp_dps_ladder: tuple[int, ...]
    rank_probe_dps: int
    singular_pivot_factor: str
    svd_max_n: int
    solution_output_dps: int
    redact_payload: bool
    log_level: str

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        cfg_path = Path(path) if path else Path(
            os.environ.get("MIPSOLVER_CONFIG", _DEFAULT_CONFIG_PATH)
        )
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
        return cls(**raw | {"mp_dps_ladder": tuple(raw["mp_dps_ladder"])})

    def with_overrides(self, overrides: dict[str, object]) -> "Config":
        unknown = set(overrides) - _OVERRIDABLE_KEYS
        if unknown:
            raise ValueError(f"unknown configuration keys: {sorted(unknown)}")
        if "mp_dps_ladder" in overrides:
            ladder = tuple(int(v) for v in overrides["mp_dps_ladder"])  # type: ignore[arg-type]
            if any(v < 4 for v in ladder):
                raise ValueError("mp_dps_ladder entries must be >= 4 (empty ladder is allowed)")
            overrides = {**overrides, "mp_dps_ladder": ladder}
        if "max_iterations_per_stage" in overrides:
            v = int(overrides["max_iterations_per_stage"])  # type: ignore[arg-type]
            if not 1 <= v <= 200:
                raise ValueError("max_iterations_per_stage must be in [1, 200]")
        if "solution_output_dps" in overrides:
            v = int(overrides["solution_output_dps"])  # type: ignore[arg-type]
            if not 4 <= v <= 200:
                raise ValueError("solution_output_dps must be in [4, 200]")
        resolved = replace(self, **overrides)
        ladder = tuple(resolved.mp_dps_ladder)
        if not (resolved.use_fp32_first or resolved.use_fp64 or ladder):
            raise ValueError(
                "at least one factorization stage must remain enabled "
                "(use_fp32_first, use_fp64, or a non-empty mp_dps_ladder)"
            )
        return resolved
