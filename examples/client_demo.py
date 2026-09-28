#!/usr/bin/env python3
"""Service-call examples using httpx (the same client the tests use).

Demonstrates: explicit rejection of unsupported constructs, the
unsatisfiable-vs-inconsistent distinction, a mutex conflict path, and request
correlation. Run while the server is up (`make run`).
"""

from __future__ import annotations

import json
import os

import httpx

BASE = os.environ.get("BASE", "http://127.0.0.1:8000")


def show(title: str, resp: httpx.Response) -> None:
    print(f"\n=== {title} ({resp.status_code}) ===")
    print(json.dumps(resp.json(), indent=2, ensure_ascii=False)[:2500])


def main() -> None:
    with httpx.Client(base_url=BASE, timeout=10) as http:
        # 1) unsupported OWL construct -> explicit 422 category, never a label
        bad = {
            "ontology_id": "demo-unsupported",
            "axioms": [
                {"type": "SubClassOf",
                 "sub": {"type": "ObjectSomeValuesFrom",
                         "property": {"type": "ObjectProperty", "name": "hasPet"},
                         "filler": {"type": "Class", "name": "Cat"}},
                 "sup": {"type": "Class", "name": "PetOwner"}}
            ],
        }
        show("unsupported construct rejected", http.post("/ontologies", json=bad))

        # 2) class unsatisfiable but ontology consistent
        onto = json.load(open("data/fixture_intersection_disjoint.json"))
        http.post("/ontologies", json=onto)
        show("unsatisfiable class / consistent ontology",
             http.post(f"/ontologies/{onto['ontology_id']}/reason"))

        # 3) real individual in disjoint classes -> ontology inconsistent
        mutex = json.load(open("data/fixture_mutex_instance.json"))
        http.post("/ontologies", json=mutex)
        show("mutex instance -> ontology inconsistent",
             http.post(f"/ontologies/{mutex['ontology_id']}/reason"))

        # 4) request correlation: a supplied id is echoed and logged
        rid = "demo-python-001"
        r = http.post(f"/ontologies/{mutex['ontology_id']}/reason",
                      headers={"x-request-id": rid})
        print("\n=== correlation ===")
        print("response header:", r.headers.get("x-request-id"))
        print("staged log events:")
        for ev in http.get(f"/requests/{rid}").json()["events"]:
            print("  ", ev["ts"], ev["stage"], ev["method"], ev["path"], ev["status_code"])


if __name__ == "__main__":
    main()
