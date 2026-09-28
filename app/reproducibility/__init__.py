"""Reproducibility: deterministic synthetic fixtures and provenance.

Every fixture here is generated from a fixed-seed PCG64 generator *or* from
explicit constants, so a result is bit-for-bit reproducible. The fixtures are
the data the independent test suite checks against; their expected answers are
computed by hand in ``tests/reference_expected.py`` and *not* produced by the
estimation core under test.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from app.config import CONFIG

from .fixtures import (
    FIXTURE_REGISTRY,
    build_fixture,
    list_fixtures,
)


@dataclass(frozen=True)
class Provenance:
    core_version: str
    seed: int
    rng_algo: str
    generated_at: str
    fixture_fingerprint: str

    def as_dict(self) -> dict[str, str | int]:
        return {
            "core_version": self.core_version,
            "seed": self.seed,
            "rng_algo": self.rng_algo,
            "generated_at": self.generated_at,
            "fixture_fingerprint": self.fixture_fingerprint,
        }


def fixture_provenance(name: str) -> Provenance:
    """Stable provenance for a generated fixture.

    The fingerprint hashes the *observation content* (ids, periods, outcomes,
    treatment, weights), not a timestamp, so identical fixtures across runs
    produce identical fingerprints.
    """
    obs = build_fixture(name)
    payload = [
        (o.unit_id, o.period, round(o.y, 12), o.treated, round(o.weight, 12))
        for o in obs
    ]
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return Provenance(
        core_version=CONFIG.core_version,
        seed=CONFIG.reproducibility.fixture_seed,
        rng_algo=CONFIG.reproducibility.rng_algo,
        generated_at=datetime.now(timezone.utc).isoformat(),
        fixture_fingerprint=digest,
    )


__all__ = [
    "FIXTURE_REGISTRY",
    "Provenance",
    "build_fixture",
    "fixture_provenance",
    "list_fixtures",
]
