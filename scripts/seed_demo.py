"""Seed a local database with the sample fixtures.

Usage:
    python -m scripts.seed_demo [db_path]

Idempotent-ish: skips problems whose id already exists.
"""

from __future__ import annotations

import sys
from pathlib import Path

from atms_backend.api.deps import init_service
from atms_backend.core.budgets import Budgets

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"

SEED_PLAN = [
    ("shared", "Shared premises, two proofs, mutual exclusion",
     "shared_reasoning.atms"),
    ("diamond", "Diamond proofs with a conflicting pair", "diamond.atms"),
    ("budget", "Budget/incompleteness demo (10 proofs)", "budget_ten.atms"),
]


def main(db_path: str = "data/atms.db") -> None:
    svc = init_service(db_path, Budgets())
    for pid, name, file_name in SEED_PLAN:
        if svc.repo.problem_exists(pid):
            print(f"skip   {pid} (exists)")
            continue
        source = (FIXTURES / file_name).read_text(encoding="utf-8")
        svc.create_problem(pid, name, source)
        _, summary = svc.propagate(pid, request_id=f"seed-{pid}")
        print(
            f"seeded {pid}: incomplete={summary['incomplete']} "
            f"nogoods={summary['nogoods']}"
        )
    print(f"database: {db_path}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/atms.db")
