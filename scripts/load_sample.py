"""Load examples/sample_session.json into a running (or fresh) session.

Usage: python -m scripts.load_sample [db_path]
Prints the resulting label of C and the nogoods, checked against the
hand-computed expectations stored in the sample file.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from app.config import Settings
from app.core.engine import ATMS
from app.core.types import Rule

SAMPLE = Path(__file__).resolve().parent.parent / "examples" / "sample_session.json"


def main() -> int:
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    engine = ATMS(Settings().budget)
    for premise in sample["premises"]:
        engine.add_premise(premise["node"])
    for assumption in sample["assumptions"]:
        engine.add_assumption(assumption["name"])
    for rule in sample["rules"]:
        engine.add_rule(
            Rule(rule["rule_id"], tuple(rule["antecedents"]), rule["consequent"])
        )

    label_c = engine.query("C")["environments"]
    nogoods = engine.known_nogoods()
    print("label(C) =", label_c)
    print("nogoods  =", nogoods)
    ok = label_c == sample["expected"]["label_C"] and nogoods == sample["expected"]["nogoods"]
    print("matches hand-computed expectation:", ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
