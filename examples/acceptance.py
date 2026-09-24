"""Standalone acceptance script: hits a running server and checks invariants.

Usage:
    uvicorn app.main:app --port 8000 &
    python -m examples.acceptance
"""
from __future__ import annotations

import argparse
import sys
import tempfile
import time
import uuid

import httpx


def check(name: str, cond: bool, detail: str = "") -> bool:
    mark = "PASS" if cond else "FAIL"
    print(f"[{mark}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    return cond


def run(base_url: str) -> int:
    fails = 0
    suffix = uuid.uuid4().hex[:8]
    # Isolated corridor far from any seeded geometry, so the acceptance
    # scenario is hermetic even when demo data is present.
    Y = 100_000.0

    def pt(x: float) -> tuple[float, float]:
        return (x, Y)

    with httpx.Client(base_url=base_url, timeout=10, trust_env=False) as c:
        # fresh operator so the script is independently repeatable
        user = f"accept-{suffix}"
        pw = "acceptance-password"
        r = c.post("/auth/register", json={"username": user, "password": pw})
        assert r.status_code in (201, 409), r.text
        token = c.post("/auth/login",
                       json={"username": user, "password": pw}
                       ).json()["access_token"]
        h = {"X-Auth-Token": token}

        def robot(rid, pos, soc, cap):
            resp = c.post("/robots", headers=h, json={
                "id": rid, "name": rid, "position": list(pos),
                "current_soc_kwh": soc, "capacity_kwh": cap})
            if resp.status_code != 201:
                print(f"  robot create {rid}: {resp.status_code} {resp.text}")
            return resp.status_code == 201

        def charger(cid, pos, cap=1):
            return c.post("/chargers", headers=h, json={
                "id": cid, "name": cid, "position": list(pos),
                "capacity": cap}).status_code == 201

        def task(tid, pickup, delivery, payload=0.0, wait=0.0, priority=0):
            return c.post("/tasks", headers=h, json={
                "id": tid, "title": tid, "pickup": list(pickup),
                "delivery": list(delivery), "payload_kg": payload,
                "wait_seconds": wait, "priority": priority}
                ).status_code == 201

        # 1. locally cheapest but cannot return
        loc, far = f"loc-{suffix}", f"far-{suffix}"
        assert robot(loc, pt(0), 4.0, 4.0)
        assert robot(far, pt(300), 300.0, 400.0)
        cw = f"cw-{suffix}"
        t1 = f"t1-{suffix}"
        assert charger(cw, pt(0))
        assert task(t1, pt(0), pt(150))
        body = c.post("/dispatch/assignments/batch", headers=h,
                      params=[("task_ids", t1)]).json()
        asns = [a for a in body["assignments"] if a["task_id"] == t1]
        winner = asns[0] if asns else None
        # invariant A: the locally-cheap, cannot-return robot must not win
        cheap_rejected = bool(winner) and winner["robot_id"] != loc
        # invariant B: it must appear among rejected alternatives with reason
        rejected = winner["rejected_alternatives"] if winner else []
        cheap_explained = any(
            e["robot_id"] == loc and e["reason"] in
            ("unreachable", "no_reachable_charger") and e["feasible"] is False
            for e in rejected)
        # invariant C: winner's own budget was reachable with margin
        winner_safe = bool(winner) and (
            winner["predicted_soc_kwh"] >= winner["required_soc_kwh"])
        fails += not check(
            "locally-cheapest unreachable robot rejected with explanation",
            cheap_rejected and cheap_explained and winner_safe,
            detail=str([(a["robot_id"], a["charger_id"]) for a in asns])
                   + " / " + str(rejected))

        # 2. charger competition: one more task on single-cap charger
        t2 = f"t2-{suffix}"
        assert task(t2, pt(0), pt(150))
        c.post("/dispatch/assignments/batch", headers=h,
               params=[("task_ids", t2)])
        active = [a for a in c.get("/assignments").json()
                  if a["status"] in ("reserved", "running", "charging")
                  and a["charger_id"] == cw]
        fails += not check(
            "charger capacity 1 never double-booked",
            len(active) == 1, detail=f"{len(active)} active reservations")

        # 3. cancel releases pre-emption, task re-allocates
        aid = winner["id"]
        rc = c.post(f"/assignments/{aid}/cancel", headers=h)
        fails += not check("cancel reservation", rc.status_code == 200,
                           detail=rc.text)
        body3 = c.post("/dispatch/assignments/batch", headers=h,
                       params=[("task_ids", t1)]).json()
        re_asns = [a for a in body3["assignments"] if a["task_id"] == t1]
        fails += not check(
            "re-allocation after cancel", len(re_asns) == 1,
            detail=str(body3.get("unassigned")))

        # 4. measured battery below prediction -> critical + blocked start
        new_id = re_asns[0]["id"]
        atok = next(a["token"] for a in c.get("/assignments").json()
                    if a["id"] == new_id)
        ah = {**h, "X-Assignment-Token": atok}
        tel = c.post(f"/assignments/{new_id}/telemetry", headers=ah,
                     json={"measured_soc_kwh": 0.5}).json()
        fails += not check(
            "measured << predicted => critical",
            tel["severity"] == "critical" and tel["blocked_start"] is True,
            detail=str(tel))
        blocked = c.post(f"/assignments/{new_id}/start", headers=ah)
        fails += not check("critical robot cannot start",
                           blocked.status_code == 409, detail=blocked.text)

        # 5. charger failure: running kept + risk alert, reserved recomputed
        assert robot(f"rb-{suffix}", pt(0), 80.0, 80.0)
        assert robot(f"rb2-{suffix}", pt(0), 80.0, 80.0)
        assert charger(f"c1-{suffix}", pt(80))
        assert charger(f"c2-{suffix}", pt(85))
        t3, t4 = f"t3-{suffix}", f"t4-{suffix}"
        assert task(t3, pt(0), pt(60))
        assert task(t4, pt(0), pt(60))
        b4 = c.post("/dispatch/assignments/batch", headers=h,
                    params=[("task_ids", t3), ("task_ids", t4)]).json()
        pair = [a for a in b4["assignments"]
                if a["task_id"] in (t3, t4)]
        assert len(pair) == 2
        running_one = next(a for a in pair if a["charger_id"] == f"c1-{suffix}")
        rtok = next(a["token"] for a in c.get("/assignments").json()
                    if a["id"] == running_one["id"])
        c.post(f"/assignments/{running_one['id']}/start",
               headers={**h, "X-Assignment-Token": rtok})
        fo = c.post(f"/chargers/c2-{suffix}/failover", headers=h).json()
        fails += not check(
            "failure releases unstarted reservation",
            len(fo["released_reservations"]) == 1, detail=str(fo))
        fo2 = c.post(f"/chargers/c1-{suffix}/failover", headers=h).json()
        fails += not check(
            "running task kept with risk alert on charger failure",
            len(fo2["risk_alerts"]) == 1
            and fo2["risk_alerts"][0]["reservation_kept"] is True,
            detail=str(fo2))

        # 6. tampered assignment token rejected
        bad = atok[:-2] + ("00" if atok[-2:] != "00" else "11")
        denied = c.post(f"/assignments/{new_id}/start",
                        headers={**h, "X-Assignment-Token": bad})
        fails += not check("forged HMAC token rejected",
                           denied.status_code == 401, detail=denied.text)

        # 7. no real control commands
        logs = c.get("/dispatch/logs").json()
        fails += not check(
            "dispatch logs are marked simulated",
            logs and all(x.get("simulated") is True for x in logs))

    print()
    if fails:
        print(f"{fails} acceptance check(s) FAILED")
        return 1
    print("All acceptance checks PASSED")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = ap.parse_args()
    for attempt in range(20):
        try:
            if httpx.Client(trust_env=False).get(
                    f"{args.base_url}/health", timeout=1).status_code == 200:
                break
        except httpx.TransportError:
            time.sleep(0.5)
    else:
        print(f"server not reachable at {args.base_url}", file=sys.stderr)
        sys.exit(2)
    sys.exit(run(args.base_url))


if __name__ == "__main__":
    main()
