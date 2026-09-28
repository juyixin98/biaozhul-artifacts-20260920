"""End-to-end example: solve, replay an invalid plan, and read evidence.

Run from the repository root:

    python examples/run_example.py

The script uses only local synthetic data (the bundled YAML fixtures) and
a throwaway SQLite database under ``data/example.db``. It prints the
concrete status, makespan, categorized violations and run identity for
every step, so it doubles as a manual smoke test.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.api.service import PlanningService  # noqa: E402
from app.config import runtime_versions  # noqa: E402
from app.rules.loader import load_file  # noqa: E402
from app.rules.models import Plan, ScheduledAction  # noqa: E402
from app.storage import EvidenceStore, connect, init_schema  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


def heading(title: str) -> None:
    print(f"\n=== {title} ===")


def main() -> None:
    print("versions:", json.dumps(runtime_versions(), sort_keys=True))

    db_path = ROOT / "data" / "example.db"
    conn = connect(db_path)
    init_schema(conn)
    service = PlanningService(EvidenceStore(conn), engine_version=runtime_versions()["service"])

    # 1) Optimal solve with a generous budget.
    heading("solve reactor (optimal)")
    reactor = load_file(FIXTURES / "reactor.yaml")
    outcome = service.solve(
        reactor,
        budget_nodes=100_000,
        max_steps=6,
        max_occurrences_per_action=None,
        cross_check=False,
        reference_max_steps=5,
        reference_max_occurrences=3,
    )
    search = outcome.search
    print(f"run_id={outcome.run_id}")
    print(f"status={search.status.value} optimal={search.optimal} "
          f"makespan={search.makespan} nodes={search.nodes_expanded}")
    if search.plan:
        print("plan:", [(s.action, f"[{s.start},{s.start + s.duration})") for s in search.plan.steps])
    print("reason:", search.reason)

    # 2) The mid-action invariant failure scenario, replayed directly.
    heading("replay invalid plan: vent drains coolant mid-pump")
    bad_plan = Plan(steps=[
        ScheduledAction(action="run_pump", start=0, duration=3),
        ScheduledAction(action="vent", start=1, duration=0),
    ])
    run_id, _fp, _version, verdict = service.replay_only(reactor, bad_plan)
    print(f"run_id={run_id} outcome={verdict.outcome.value} goal_satisfied={verdict.goal_satisfied}")
    for violation in verdict.violations:
        print(f"  - t={violation.time} {violation.category}: {violation.message}")

    # 3) Budget expiry: feasible plan, optimality unproven.
    heading("solve workshop under a tight budget (feasible, optimality unproven)")
    workshop = load_file(FIXTURES / "workshop.yaml")
    tight = service.solve(
        workshop,
        budget_nodes=800,
        max_steps=8,
        max_occurrences_per_action=None,
        cross_check=False,
        reference_max_steps=6,
        reference_max_occurrences=3,
    )
    tight_search = tight.search
    print(f"run_id={tight.run_id}")
    print(f"status={tight_search.status.value} optimal={tight_search.optimal} "
          f"makespan={tight_search.makespan} nodes={tight_search.nodes_expanded}")
    print("reason:", tight_search.reason)

    # 4) Evidence is retrievable afterwards from SQLite by run identity.
    heading("evidence retrieval from SQLite")
    record = service.store.get_run(run_id)
    print(f"stored replay status={record['outcome']} fingerprint={record['input_fingerprint'][:16]}...")
    events = service.store.get_events(run_id)
    zero = [e for e in events if e["kind"] == "ZERO_DURATION"]
    print(f"{len(events)} timeline events persisted; {len(zero)} zero-duration event(s)")

    conn.close()
    print("\nExample finished. Inspect", db_path)


if __name__ == "__main__":
    main()
