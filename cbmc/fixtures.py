"""Bundled contract fixtures: safe transfer, buggy contracts, edge cases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ContractError
from .model import Contract, load_contract

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"

#: fixture id -> (file, one-line summary)
FIXTURES: dict[str, tuple[str, str]] = {
    "safe-transfer": (
        "safe_transfer.json",
        "Guarded token transfer + escrow deposit; non-negativity and "
        "conservation hold within the bound.",
    ),
    "escrow-missing-debit": (
        "escrow_missing_debit.json",
        "settle() credits the seller but never debits escrow: shortest "
        "counterexample breaks conservation at step 2.",
    ),
    "uint8-pool-overflow": (
        "uint8_pool_overflow.json",
        "8-bit unchecked arithmetic wraps modulo 256; widened-sum comparison "
        "detects the overflow.",
    ),
    "unreachable-target": (
        "unreachable_target.json",
        "One named target is unsatisfiable (UNSAT within bound), another is "
        "reachable in one step.",
    ),
    "piggy-step-boundary": (
        "piggy_step_boundary.json",
        "'smashed' needs exactly 15 deposits: demonstrates the steps bound and "
        "shortest-depth minimality.",
    ),
}


def fixture_path(fixture_id: str) -> Path:
    if fixture_id not in FIXTURES:
        raise ContractError(
            f"unknown fixture {fixture_id!r}; known: {', '.join(sorted(FIXTURES))}"
        )
    path = EXAMPLES_DIR / FIXTURES[fixture_id][0]
    if not path.exists():
        raise ContractError(f"fixture file missing: {path}")
    return path


def load_example(fixture_id: str) -> Contract:
    """Load and validate one of the bundled fixtures."""
    return load_contract(json.loads(fixture_path(fixture_id).read_text("utf-8")))


def get_fixture(fixture_id: str) -> dict[str, Any]:
    """Return the raw JSON document of a bundled fixture."""
    return json.loads(fixture_path(fixture_id).read_text("utf-8"))


def list_fixtures() -> list[dict[str, str]]:
    return [
        {"id": fixture_id, "file": file_name, "summary": summary}
        for fixture_id, (file_name, summary) in sorted(FIXTURES.items())
    ]
