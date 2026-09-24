"""End-to-end required scenarios for the allocation API."""
from __future__ import annotations


def add_robot(client, rid, pos, soc, cap, payload_cap=1000.0):
    r = client.post("/robots", json={
        "id": rid, "name": rid, "position": list(pos),
        "current_soc_kwh": soc, "capacity_kwh": cap,
        "payload_capacity_kg": payload_cap,
    })
    assert r.status_code == 201, r.text


def add_charger(client, cid, pos, capacity=1):
    r = client.post("/chargers", json={
        "id": cid, "name": cid, "position": list(pos),
        "capacity": capacity,
    })
    assert r.status_code == 201, r.text


def add_task(client, tid, pickup, delivery, payload=0.0, wait=0.0, priority=0):
    r = client.post("/tasks", json={
        "id": tid, "title": tid, "pickup": list(pickup),
        "delivery": list(delivery), "payload_kg": payload,
        "wait_seconds": wait, "priority": priority,
    })
    assert r.status_code == 201, r.text


def get_assignment_token(client, assignment_id):
    rows = client.get("/assignments").json()
    row = next(a for a in rows if a["id"] == assignment_id)
    return row["token"]


# ---------------------------------------------------------------------------
# Scenario 1: locally cheapest robot cannot return to any charger
# ---------------------------------------------------------------------------

def test_01_locally_cheapest_but_cannot_return(client):
    # Local robot sits right at pickup -> zero outbound, but tiny battery
    # means it cannot reach a charger after delivery.
    add_robot(client, "R-local", pos=(0, 0), soc=4.0, cap=4.0)
    # Far-away healthy robot: expensive outbound but enough battery to return.
    add_robot(client, "R-far", pos=(300, 0), soc=300.0, cap=400.0)
    add_charger(client, "C-west", pos=(0, 0))

    add_task(client, "T1", pickup=(0, 0), delivery=(150, 0))

    # Preview must flag the local robot as unreachable despite low trip cost.
    preview = client.post("/tasks/preview-cost", json={
        "robot_pos": [0, 0], "current_soc_kwh": 4.0, "capacity_kwh": 4.0,
        "pickup": [0, 0], "delivery": [150, 0],
        "payload_kg": 0, "wait_seconds": 0, "priority": 0,
    }).json()
    assert preview["feasible"] is False
    assert preview["reason"] == "unreachable"

    resp = client.post("/dispatch/assignments/batch")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["simulated"] is True
    assert len(body["assignments"]) == 1
    asn = body["assignments"][0]
    assert asn["robot_id"] == "R-far", (
        "dispatcher must reject the locally-cheap robot that cannot return")
    assert asn["charger_id"] == "C-west"
    assert asn["status"] == "reserved"

    # The local robot shows up as a rejected alternative with a reason.
    entry = next(e for e in asn["rejected_alternatives"]
                 if e["robot_id"] == "R-local")
    assert entry["feasible"] is False
    assert entry["reason"] in ("unreachable", "no_reachable_charger")


# ---------------------------------------------------------------------------
# Scenario 2: charger competition
# ---------------------------------------------------------------------------

def test_02_charger_competition(client):
    # Both robots can do the task; both can only return to the SAME single
    # capacity-1 charger. Exactly one reservation must win.
    add_robot(client, "RA", pos=(0, 0), soc=50.0, cap=50.0)
    add_robot(client, "RB", pos=(10, 0), soc=50.0, cap=50.0)
    add_charger(client, "C0", pos=(100, 0), capacity=1)
    add_task(client, "J1", pickup=(0, 0), delivery=(60, 0))

    body = client.post("/dispatch/assignments/batch").json()
    assert len(body["assignments"]) == 1
    winner = body["assignments"][0]
    assert winner["charger_id"] == "C0"

    # A second task now: no charger capacity left -> blocked, no double booking.
    add_task(client, "J2", pickup=(0, 0), delivery=(60, 0))
    body2 = client.post("/dispatch/assignments/batch").json()
    assert len(body2["assignments"]) == 0
    reasons = {e["robot_id"]: e["reason"]
               for e in body2["unassigned"][0]["per_robot"]}
    # the still-idle robot is contention-blocked by the first reservation
    loser = "RB" if winner["robot_id"] == "RA" else "RA"
    assert reasons[loser] == "blocked_by_charger_contention"

    # charger must show exactly one active reservation
    active = [a for a in client.get("/assignments").json()
              if a["status"] in ("reserved", "running", "charging")]
    assert sum(1 for a in active if a["charger_id"] == "C0") == 1


def test_02b_second_charger_resolves_competition(client):
    add_robot(client, "RA", pos=(0, 0), soc=50.0, cap=50.0)
    add_robot(client, "RB", pos=(10, 0), soc=50.0, cap=50.0)
    add_charger(client, "C0", pos=(100, 0), capacity=1)
    add_charger(client, "C1", pos=(70, 0), capacity=1)
    add_task(client, "J1", pickup=(0, 0), delivery=(60, 0))
    add_task(client, "J2", pickup=(0, 0), delivery=(60, 0))

    body = client.post("/dispatch/assignments/batch").json()
    assert len(body["assignments"]) == 2
    chargers = {a["robot_id"]: a["charger_id"] for a in body["assignments"]}
    assert len(set(chargers.values())) == 2, "each robot gets a distinct charger"


# ---------------------------------------------------------------------------
# Scenario 3: cancel releases battery/charger pre-emption
# ---------------------------------------------------------------------------

def test_03_cancel_releases_reservation(client):
    add_robot(client, "R1", pos=(0, 0), soc=50.0, cap=50.0)
    add_charger(client, "C1", pos=(100, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))

    asn = client.post("/dispatch/assignments/batch").json()["assignments"][0]
    assert client.get(f"/robots/R1").json()["status"] == "reserved"

    cancelled = client.post(f"/assignments/{asn['id']}/cancel")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"

    # robot idle again, task pending again, charger slot free
    assert client.get(f"/robots/R1").json()["status"] == "idle"
    assert client.get("/tasks").json()[0]["status"] == "pending"
    log = client.get("/dispatch/logs").json()[0]
    assert log["simulated"] is True
    assert log["action"] == "cancel_simulated"

    # can immediately accept the task again
    body = client.post("/dispatch/assignments/batch").json()
    assert len(body["assignments"]) == 1

    # cannot cancel twice
    again = client.post(f"/assignments/{asn['id']}/cancel")
    assert again.status_code == 409


# ---------------------------------------------------------------------------
# Scenario 4: measured battery below prediction
# ---------------------------------------------------------------------------

def test_04_measured_battery_below_prediction(client):
    add_robot(client, "R1", pos=(0, 0), soc=50.0, cap=50.0)
    add_charger(client, "C1", pos=(100, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    asn = client.post("/dispatch/assignments/batch").json()["assignments"][0]
    token = get_assignment_token(client, asn["id"])
    predicted = asn["predicted_soc_kwh"]

    # mild deviation -> warning, start still allowed
    mild = client.post(f"/assignments/{asn['id']}/telemetry",
                       headers={"X-Assignment-Token": token},
                       json={"measured_soc_kwh": round(predicted * 0.95, 3)})
    assert mild.status_code == 200, mild.text
    assert mild.json()["severity"] == "warning"

    # severe deviation -> critical, robot at risk, start blocked
    crit = client.post(f"/assignments/{asn['id']}/telemetry",
                       headers={"X-Assignment-Token": token},
                       json={"measured_soc_kwh": round(predicted * 0.7, 3)})
    assert crit.json()["severity"] == "critical"
    assert crit.json()["blocked_start"] is True
    assert client.get(f"/robots/R1").json()["at_risk"] is True

    blocked = client.post(f"/assignments/{asn['id']}/start",
                          headers={"X-Assignment-Token": token})
    assert blocked.status_code == 409

    alerts = client.get("/alerts", params={"severity": "critical"}).json()
    assert any(a["kind"] == "battery_risk" for a in alerts)

    # a fresh measurement at/above expectation cannot silently clear risk
    # while the critical record stands this turn (operator must cancel/replan)
    ok = client.post(f"/assignments/{asn['id']}/telemetry",
                     headers={"X-Assignment-Token": token},
                     json={"measured_soc_kwh": predicted})
    assert ok.status_code == 200 and ok.json()["severity"] == "ok"


def test_04b_healthy_measurement_allows_start(client):
    add_robot(client, "R1", pos=(0, 0), soc=50.0, cap=50.0)
    add_charger(client, "C1", pos=(100, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    asn = client.post("/dispatch/assignments/batch").json()["assignments"][0]
    token = get_assignment_token(client, asn["id"])
    started = client.post(f"/assignments/{asn['id']}/start",
                          headers={"X-Assignment-Token": token})
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "running"


# ---------------------------------------------------------------------------
# Scenario 5: charger failure — recompute unstarted, keep running + risk alert
# ---------------------------------------------------------------------------

def test_05_charger_failure_recompute(client):
    add_robot(client, "RA", pos=(0, 0), soc=60.0, cap=60.0)
    add_robot(client, "RB", pos=(0, 0), soc=60.0, cap=60.0)
    add_charger(client, "C1", pos=(80, 0), capacity=1)
    add_charger(client, "C2", pos=(90, 0), capacity=1)
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    add_task(client, "T2", pickup=(0, 0), delivery=(60, 0))
    body = client.post("/dispatch/assignments/batch").json()
    assert len(body["assignments"]) == 2

    by_charger = {a["charger_id"]: a for a in body["assignments"]}
    running_asn = by_charger["C1"]
    reserved_asn = by_charger["C2"]
    rtoken = get_assignment_token(client, running_asn["id"])
    assert client.post(f"/assignments/{running_asn['id']}/start",
                       headers={"X-Assignment-Token": rtoken}
                       ).status_code == 200

    # Fail C2 (holds the unstarted reservation): it must be rolled back and
    # the task recomputed onto C1 — but C1's slot is held by the running task,
    # so T2 stays unassigned with contention explanation.
    out = client.post(f"/chargers/C2/failover").json()
    assert out["simulated"] is True
    assert len(out["released_reservations"]) == 1
    assert out["released_reservations"][0]["assignment_id"] == reserved_asn["id"]
    assert out["unassigned_after_recompute"][0]["task_id"] == "T2"

    # Fail C1 (running task): reservation KEPT, critical risk alert raised.
    out2 = client.post(f"/chargers/C1/failover").json()
    assert len(out2["risk_alerts"]) == 1
    alert = out2["risk_alerts"][0]
    assert alert["reservation_kept"] is True
    assert alert["assignment_id"] == running_asn["id"]
    assert any(x["charger_id"] == "C2" and not x["reachable"]
               for x in alert["alternatives"]) is False
    # C2 failed too -> no reachable *available* charger: alert explains that.
    assert alert["safe_alternatives"] == []

    running = client.get("/assignments", params={"status": "running"}).json()
    assert [a["id"] for a in running] == [running_asn["id"]]


def test_05b_failure_reroutes_to_alternative_charger(client):
    add_robot(client, "RA", pos=(0, 0), soc=80.0, cap=80.0)
    add_robot(client, "RB", pos=(0, 0), soc=80.0, cap=80.0)
    add_charger(client, "C1", pos=(80, 0))
    add_charger(client, "C2", pos=(90, 0))
    add_charger(client, "C3", pos=(85, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    add_task(client, "T2", pickup=(0, 0), delivery=(60, 0))
    body = client.post("/dispatch/assignments/batch").json()
    assert len(body["assignments"]) == 2
    by_charger = {a["charger_id"]: a for a in body["assignments"]}
    # nearest-first slot claiming: C1 (20m) and C3 (25m) are taken, C2 spare
    assert set(by_charger) == {"C1", "C3"}

    # start the one on C3 so it stays, then fail the unstarted robot's charger
    started = by_charger["C3"]
    tok = get_assignment_token(client, started["id"])
    client.post(f"/assignments/{started['id']}/start",
                headers={"X-Assignment-Token": tok})

    out = client.post("/chargers/C1/failover").json()
    # freed task must be recomputed onto the remaining free charger C2
    assert len(out["reassignments"]) == 1
    assert out["reassignments"][0]["charger_id"] == "C2"
    assert out["reassignments"][0]["task_id"] != started["task_id"]


# ---------------------------------------------------------------------------
# Scenario 6: crypto is real — tampered/expired tokens rejected
# ---------------------------------------------------------------------------

def test_06_signed_token_tampering(client):
    add_robot(client, "R1", pos=(0, 0), soc=50.0, cap=50.0)
    add_charger(client, "C1", pos=(100, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    asn = client.post("/dispatch/assignments/batch").json()["assignments"][0]

    forged = asn["token"][:-2] + ("aa" if asn["token"][-2:] != "aa" else "bb")
    resp = client.post(f"/assignments/{asn['id']}/start",
                       headers={"X-Assignment-Token": forged})
    assert resp.status_code == 401
    assert "signature" in resp.json()["detail"]

    # garbage / wrong-kind tokens
    assert client.post(
        f"/assignments/{asn['id']}/start",
        headers={"X-Assignment-Token": "not-a-token"}).status_code == 401

    # operator bearer token is not an assignment token
    op_token = client.headers["X-Auth-Token"]
    resp2 = client.post(f"/assignments/{asn['id']}/start",
                        headers={"X-Assignment-Token": op_token})
    assert resp2.status_code == 403

    # expired token
    from app import crypto
    expired = crypto.issue_token(
        {"sub": asn["id"], "kind": "assignment"}, ttl_seconds=-10)
    resp3 = client.post(f"/assignments/{asn['id']}/start",
                        headers={"X-Assignment-Token": expired})
    assert resp3.status_code == 401
    assert "expired" in resp3.json()["detail"]


def test_06b_password_hashing_is_real(client):
    from app import crypto

    h = crypto.hash_password("correct horse battery staple")
    assert h.startswith("pbkdf2_sha256$240000$")
    assert "correct horse" not in h
    assert crypto.verify_password("correct horse battery staple", h)
    assert not crypto.verify_password("wrong password", h)
    # same password -> different salt -> different hash
    h2 = crypto.hash_password("correct horse battery staple")
    assert h != h2
    assert crypto.verify_password("correct horse battery staple", h2)


# ---------------------------------------------------------------------------
# Bonus: no double acceptance / state machine / auth required for writes
# ---------------------------------------------------------------------------

def test_robot_cannot_accept_two_tasks(client):
    add_robot(client, "R1", pos=(0, 0), soc=50.0, cap=50.0)
    add_charger(client, "C1", pos=(100, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    add_task(client, "T2", pickup=(0, 0), delivery=(60, 0))

    first = client.post("/dispatch/assignments/batch").json()
    assert len(first["assignments"]) == 1
    second = client.post("/dispatch/assignments/batch").json()
    assert len(second["assignments"]) == 0
    assert second["unassigned"][0]["reason"] in (
        "no_idle_robot", "charger_contention", "no_feasible_robot")
    assert any(e["reason"] == "robot_not_idle"
               for e in second["unassigned"][0]["per_robot"])

    active = [a for a in client.get("/assignments").json()
              if a["status"] in ("reserved", "running", "charging")]
    assert len(active) == 1


def test_write_requires_operator_token(client):
    client.headers.clear()
    resp = client.post("/robots", json={
        "id": "X", "name": "X", "position": [0, 0],
        "current_soc_kwh": 1, "capacity_kwh": 1})
    assert resp.status_code == 401


def test_lifecycle_complete_and_unplug(client):
    add_robot(client, "R1", pos=(0, 0), soc=60.0, cap=60.0)
    add_charger(client, "C1", pos=(80, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    asn = client.post("/dispatch/assignments/batch").json()["assignments"][0]
    token = asn["token"]
    client.post(f"/assignments/{asn['id']}/start",
                headers={"X-Assignment-Token": token})
    done = client.post(f"/assignments/{asn['id']}/complete",
                       headers={"X-Assignment-Token": token}).json()
    assert done["status"] == "charging"
    assert client.get(f"/robots/R1").json()["status"] == "charging"
    unplugged = client.post(f"/assignments/{asn['id']}/unplug",
                            headers={"X-Assignment-Token": token}).json()
    assert unplugged["status"] == "done"
    assert client.get(f"/robots/R1").json()["status"] == "done"


def test_no_real_control_command_in_logs(client):
    add_robot(client, "R1", pos=(0, 0), soc=50.0, cap=50.0)
    add_charger(client, "C1", pos=(100, 0))
    add_task(client, "T1", pickup=(0, 0), delivery=(60, 0))
    client.post("/dispatch/assignments/batch")
    logs = client.get("/dispatch/logs").json()
    assert logs, "a simulated dispatch log should exist"
    for entry in logs:
        assert entry["simulated"] is True
        assert "velocity" not in str(entry).lower()
        assert "motor cmd" not in str(entry).lower()
