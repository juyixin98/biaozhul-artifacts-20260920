"""Shared factory helpers for tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


def create_user(client, email: str, name: str, role: str) -> int:
    resp = client.post("/users", json={"email": email, "name": name, "role": role})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def manager(client, n: int = 1) -> int:
    return create_user(client, f"manager{n}@example.test", f"Manager {n}", "MANAGER")


def learner(client, n: int = 1) -> int:
    return create_user(client, f"learner{n}@example.test", f"Learner {n}", "LEARNER")


def headers(user_id: int) -> dict:
    return {"X-User-Id": str(user_id)}


def make_steps(*prereq_specs) -> list[dict]:
    """Build simple ordered steps. Each arg is a list of prerequisite orders."""
    return [
        {
            "title": f"Step {i}",
            "description": f"description {i}",
            "pass_criteria": f"criteria {i}",
            "prerequisite_orders": list(pres),
        }
        for i, pres in enumerate(prereq_specs, start=1)
    ]


def create_published_program(client, mgr_id: int, steps: list[dict] | None = None):
    pid = client.post(
        "/programs", json={"title": "P", "description": "d"}, headers=headers(mgr_id)
    ).json()["id"]
    steps = steps if steps is not None else make_steps([], [1], [1, 2])
    vid = client.post(
        f"/programs/{pid}/versions", json={"steps": steps}, headers=headers(mgr_id)
    ).json()["id"]
    client.post(f"/versions/{vid}/publish", headers=headers(mgr_id))
    return pid, vid


def create_course(
    client,
    mgr_id: int,
    version_id: int,
    capacity: int = 5,
    deadline: datetime | None = None,
) -> int:
    deadline = deadline or (datetime.now(timezone.utc) + timedelta(days=7))
    resp = client.post(
        "/courses",
        json={
            "version_id": version_id,
            "capacity": capacity,
            "enrollment_deadline": deadline.isoformat(),
        },
        headers=headers(mgr_id),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def set_clock(client, mgr_id: int, when: datetime) -> None:
    resp = client.put("/admin/clock", json={"now": when.isoformat()}, headers=headers(mgr_id))
    assert resp.status_code == 200, resp.text


def advance(client, mgr_id: int, **delta) -> None:
    resp = client.post("/admin/clock/advance", json=delta, headers=headers(mgr_id))
    assert resp.status_code == 200, resp.text


def reset_clock(client, mgr_id: int) -> None:
    client.post("/admin/clock/reset", headers=headers(mgr_id))
