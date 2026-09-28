"""Application configuration.

All settings are local (SQLite path, fixture directory, comparison semantics).
No production accounts or external services are involved. Values can be
overridden through environment variables so the same code runs under tests with
an isolated temporary database.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    db_path: Path
    fixture_dir: Path
    # Three-valued logic (SQL-style) is the *only* supported NULL policy for
    # comparisons. NULL operands make a predicate INDETERMINATE, and such rows
    # are excluded from the positive answer (they are never silently matched).
    null_policy: str = "sql_three_valued"
    # Union (UNION ALL) keeps duplicates; bag-semantics provenance keeps them
    # as additive terms. DISTINCT is a separate, explicit operator.
    bag_union_default: bool = True
    log_redact_secrets: bool = True

    @staticmethod
    def from_env() -> "Settings":
        db_path = Path(_env("PROVENANCE_DB", str(REPO_ROOT / "data" / "provenance.db")))
        fixture_dir = Path(_env("PROVENANCE_FIXTURES", str(REPO_ROOT / "fixtures")))
        policy = _env("PROVENANCE_NULL_POLICY", "sql_three_valued")
        if policy != "sql_three_valued":
            # Deliberately narrow supported scope rather than guessing.
            raise ValueError(f"unsupported NULL policy: {policy!r}")
        return Settings(db_path=db_path, fixture_dir=fixture_dir)


settings = Settings.from_env()
