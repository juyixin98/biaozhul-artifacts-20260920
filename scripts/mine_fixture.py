#!/usr/bin/env python3
"""Local verification CLI: run every fixture case through the mining kernel
and check the output against the hand-computed expectations in fixtures/.

Usage:
    python scripts/mine_fixture.py                 # all fixtures, all cases
    python scripts/mine_fixture.py --fixture time_gap

Exit code 0 = every case matched the reference; 1 = at least one mismatch.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.corpus.fixtures import list_fixtures, load_fixture  # noqa: E402
from app.corpus.schema import CorpusSpec  # noqa: E402
from app.logging_setup import setup_logging  # noqa: E402
from app.mining.kernel import PrefixGrowthMiner  # noqa: E402
from app.models.domain import GapConstraints  # noqa: E402


def check_case(fixture: dict, case: dict) -> list[str]:
    spec = CorpusSpec(**fixture["corpus"])
    params = case["params"]
    miner = PrefixGrowthMiner(
        spec.to_domain(),
        GapConstraints(
            max_pos_gap=params.get("max_pos_gap"),
            max_time_gap=params.get("max_time_gap"),
        ),
        min_support=params["min_support"],
    )
    results = miner.mine()
    actual = {">".join(r.pattern): r for r in results}
    errors: list[str] = []

    for pattern, expected in case["expected"].items():
        result = actual.get(pattern)
        if result is None:
            errors.append(f"pattern {pattern}: expected support "
                          f"{expected['support']} but pattern is absent")
            continue
        if result.support != expected["support"]:
            errors.append(f"pattern {pattern}: support {result.support} "
                          f"!= expected {expected['support']}")
        got: dict[str, list[list[int]]] = {}
        for ev in result.embeddings:
            got.setdefault(ev.sequence_id, []).append(list(ev.positions))
        got = {sid: sorted(v) for sid, v in got.items()}
        want = {sid: sorted(v) for sid, v in expected["embeddings"].items()}
        if got != want:
            errors.append(f"pattern {pattern}: embeddings {got} != expected {want}")

    for pattern in case.get("expected_absent", []):
        if pattern in actual:
            errors.append(f"pattern {pattern}: expected absent but frequent "
                          f"(support {actual[pattern].support})")

    unexpected = set(actual) - set(case["expected"])
    if unexpected:
        errors.append(f"unexpected frequent patterns: {sorted(unexpected)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", help="run only this fixture (name without .json)")
    parser.add_argument("--quiet", action="store_true", help="suppress kernel logs")
    args = parser.parse_args()

    setup_logging("WARNING" if args.quiet else "INFO")

    names = [args.fixture] if args.fixture else list_fixtures()
    failures = 0
    for name in names:
        fixture = load_fixture(name)
        for case in fixture["cases"]:
            errors = check_case(fixture, case)
            label = f"{name}:{case['label']}"
            if errors:
                failures += 1
                print(f"FAIL {label}")
                for err in errors:
                    print(f"  - {err}")
            else:
                print(f"PASS {label}")
    total = sum(len(load_fixture(n)["cases"]) for n in names)
    print(f"\n{total - failures}/{total} cases passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
