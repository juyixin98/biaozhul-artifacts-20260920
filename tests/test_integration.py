"""End-to-end integration tests with real ROS 2 participants and FastAPI.

Scenarios required by the task:
  * unicast to one of two robots,
  * robot reconnection (latched TRANSIENT_LOCAL delivery after respawn),
  * same relative topic name in two namespaces stays isolated,
  * unauthorized / cross-robot requests are refused,
  * expired commands are dropped with an audited reason,
  * rollback / duplicate sequences are refused,
  * mapping change invalidates old tokens immediately and revokes streams,
  * queries and SSE event subscriptions stay namespace-isolated.

Nothing ROS-related is mocked: the bridge runs a real rclpy context+executor
and each synthetic robot is a second real rclpy context+executor; endpoints
match through real DDS discovery on an isolated domain id.

Tests are *synchronous* and hop onto the single shared event loop via the
``arun`` fixture for the async SSE pieces, so ASGI app, HTTP client and event
bus all share one loop (cross-loop asyncio.Queue would deadlock otherwise).
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time

import httpx
import pytest

from conftest import wait_for_subscribers, wait_for_messages, ADMIN_TOKEN

TOPIC = "cmd/move"


def _cmd(seq, ttl=30.0, payload=None, tester_id="tester-1", target=TOPIC):
    return {
        "target": target,
        "sequence": seq,
        "ttl_seconds": ttl,
        "tester_id": tester_id,
        "payload": payload if payload is not None else {"step": seq},
    }


def _register(client, hdr, rid, ns, prewarm=(TOPIC,)):
    r = client.post(f"/admin/robots/{rid}",
                    json={"namespace": ns, "prewarm_topics": list(prewarm)},
                    headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()


def _token(client, hdr, rid, tester="tester-1"):
    r = client.post(f"/admin/robots/{rid}/tokens",
                    json={"tester_id": tester}, headers=hdr)
    assert r.status_code == 200, r.text
    return r.json()["token"]


# --------------------------------------------------------------------------- #
# 1. Unicast to exactly one of two robots
# --------------------------------------------------------------------------- #


def test_unicast_reaches_only_addressed_robot(client, gw, robots, admin_headers):
    # uni_a / uni_b are registered once by the session _shared_registrations.
    fqn_a, fqn_b = "/team/uni_a/cmd/move", "/team/uni_b/cmd/move"

    ra = robots("uni_a_node", "team/uni_a")
    rb = robots("uni_b_node", "team/uni_b")
    assert wait_for_subscribers(gw, fqn_a)
    assert wait_for_subscribers(gw, fqn_b)

    tok = _token(client, admin_headers, "uni_a")
    resp = client.post("/robots/uni_a/commands", json=_cmd(1001),
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["topic"] == fqn_a and body["subscriber_count"] == 1

    got_a = wait_for_messages(ra, 1)
    assert len(got_a) == 1
    assert got_a[0]["payload"] == {"step": 1001}
    assert got_a[0]["ns"] == "team/uni_a"
    assert got_a[0]["tester_id"] == "tester-1"

    time.sleep(0.6)
    assert rb.received() == []


# --------------------------------------------------------------------------- #
# 2. Same relative topic name in two namespaces stays isolated
# --------------------------------------------------------------------------- #


def test_same_relative_topic_name_is_namespace_isolated(
        client, gw, robots, admin_headers):
    _register(client, admin_headers, "iso_a", "team/iso_a")
    _register(client, admin_headers, "iso_b", "team/iso_b")
    fqn_a, fqn_b = "/team/iso_a/cmd/move", "/team/iso_b/cmd/move"

    ra = robots("iso_a_node", "team/iso_a")
    rb = robots("iso_b_node", "team/iso_b")
    assert wait_for_subscribers(gw, fqn_a)
    assert wait_for_subscribers(gw, fqn_b)

    tok_b = _token(client, admin_headers, "iso_b")
    resp = client.post("/robots/iso_b/commands",
                       json=_cmd(1, payload={"to": "iso_b"}),
                       headers={"Authorization": f"Bearer {tok_b}"})
    assert resp.status_code == 200
    assert resp.json()["topic"] == fqn_b
    tok_a = _token(client, admin_headers, "iso_a")
    client.post("/robots/iso_a/commands",
                json=_cmd(1, payload={"to": "iso_a"}),
                headers={"Authorization": f"Bearer {tok_a}"})

    wait_for_messages(ra, 1)
    wait_for_messages(rb, 1)
    time.sleep(0.6)
    assert {m["ns"] for m in ra.received()} == {"team/iso_a"}
    assert {m["ns"] for m in rb.received()} == {"team/iso_b"}


# --------------------------------------------------------------------------- #
# 3. Reconnection: late / respawned node receives the latched command
# --------------------------------------------------------------------------- #


def test_reconnect_late_subscriber_receives_latched_command(
        client, gw, robots, admin_headers):
    _register(client, admin_headers, "recon", "team/recon")
    fqn = "/team/recon/cmd/move"

    tok = _token(client, admin_headers, "recon")
    resp = client.post("/robots/recon/commands",
                       json=_cmd(1, payload={"queued": True}),
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 200
    assert resp.json()["subscriber_count"] == 0

    late = robots("recon_late", "team/recon")
    got = wait_for_messages(late, 1)
    assert got and got[0]["payload"] == {"queued": True}

    # Respawn: DDS still hands the new participant the last latched message.
    late.stop()
    again = robots("recon_again", "team/recon")
    got2 = wait_for_messages(again, 1)
    assert got2 and got2[0]["payload"] == {"queued": True}


# --------------------------------------------------------------------------- #
# 4. Unauthenticated, cross-robot and forged requests
# --------------------------------------------------------------------------- #


def test_unauthenticated_command_is_rejected(client):
    resp = client.post("/robots/uni_a/commands", json=_cmd(99))
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "missing_bearer_token"


def test_token_for_a_cannot_command_b(client, admin_headers):
    tok_a = _token(client, admin_headers, "uni_a")
    resp = client.post("/robots/uni_b/commands", json=_cmd(50),
                       headers={"Authorization": f"Bearer {tok_a}"})
    assert resp.status_code == 401
    assert resp.json()["detail"]["reason"] == "token_signature_invalid"


def test_forged_token_is_rejected(client, admin_headers):
    payload = ("eyJraWQiOiJtcjEiLCJyaWQiOiJ1bmlfYSIsInN1YiI6InQiLCJlcG9jaCI"
               "6MSwiZXhwIjo5OTk5OTk5OTk5fQ")
    forged = f"mr1.{payload}.AAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    resp = client.post("/robots/uni_a/commands", json=_cmd(51),
                       headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_token_for_unknown_robot_is_404(client, admin_headers):
    r = client.post("/admin/robots/ghost/tokens",
                    json={"tester_id": "t"}, headers=admin_headers)
    assert r.status_code == 404


def test_tester_identity_mismatch_is_forbidden(client, admin_headers):
    tok = _token(client, admin_headers, "uni_a", tester="tester-1")
    resp = client.post("/robots/uni_a/commands",
                       json=_cmd(60, tester_id="tester-2"),
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 403
    assert resp.json()["reason"] == "tester_mismatch"


# --------------------------------------------------------------------------- #
# 5. Topic escape attempts at the HTTP boundary
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("evil", [
    "/team/uni_b/cmd/move",   # absolute -> jump straight into another ns
    "/cmd/move",              # graph-root absolute
    "../uni_b/cmd/move",      # relative traversal
    "~/secret",               # private-name escape
])
def test_topic_escape_is_rejected(client, gw, admin_headers, evil):
    tok = _token(client, admin_headers, "uni_a")
    before = set(gw.bridge.status()["publishers"])
    resp = client.post("/robots/uni_a/commands", json=_cmd(70, target=evil),
                       headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 400, resp.text
    # Security invariant: the rejected request created NO new publisher.
    # (Legitimate prewarmed publishers for other namespaces may already exist,
    # but nothing foreign is created as a side effect of the rejected call.)
    after = set(gw.bridge.status()["publishers"])
    assert after == before, f"unexpected publisher change: {after ^ before}"


# --------------------------------------------------------------------------- #
# 6. Expired commands dropped with recorded reason
# --------------------------------------------------------------------------- #


def test_expired_command_is_dropped_and_audited(
        client, gw, robots, admin_headers):
    _register(client, admin_headers, "exp", "team/exp")
    rd = robots("exp_node", "team/exp")
    assert wait_for_subscribers(gw, "/team/exp/cmd/move")

    tok = _token(client, admin_headers, "exp")
    past = time.time() - 1
    resp = client.post(
        "/robots/exp/commands",
        json={"target": TOPIC, "sequence": 1, "expires_at": past,
              "tester_id": "tester-1", "payload": {"late": True}},
        headers={"Authorization": f"Bearer {tok}"})
    assert resp.status_code == 400
    assert resp.json()["reason"] == "command_expired"

    time.sleep(0.4)
    assert all(m.get("payload") != {"late": True} for m in rd.received())

    with open(gw.settings.audit_log_path, encoding="utf-8") as fh:
        audit = [json.loads(line) for line in fh if line.strip()]
    expired = [e for e in audit if e.get("reason") == "command_expired"]
    assert expired and expired[-1]["target"] == TOPIC
    assert expired[-1]["message"]


# --------------------------------------------------------------------------- #
# 7. Sequence never rolls back / no duplicate replay
# --------------------------------------------------------------------------- #


def test_sequence_rollback_and_duplicate_rejected(
        client, gw, robots, admin_headers):
    _register(client, admin_headers, "seq", "team/seq")
    robots("seq_node", "team/seq")
    assert wait_for_subscribers(gw, "/team/seq/cmd/move")
    tok = _token(client, admin_headers, "seq")
    h = {"Authorization": f"Bearer {tok}"}

    def post(seq):
        return client.post("/robots/seq/commands", json=_cmd(seq), headers=h)

    assert post(10).status_code == 200
    assert post(12).status_code == 200

    back = post(11)
    assert back.status_code == 409
    assert back.json()["reason"] == "sequence_rollback"
    assert back.json()["detail"]["high_water"] == 12

    dup = post(12)
    assert dup.status_code == 409
    assert dup.json()["reason"] == "sequence_duplicate"

    assert post(13).status_code == 200


# --------------------------------------------------------------------------- #
# 8. Mapping change immediately invalidates old authorization
# --------------------------------------------------------------------------- #


def test_remap_immediately_stales_old_token(client, gw, admin_headers):
    _register(client, admin_headers, "remap", "team/remap")
    old_tok = _token(client, admin_headers, "remap")
    h = {"Authorization": f"Bearer {old_tok}"}
    assert client.post("/robots/remap/commands", json=_cmd(1),
                       headers=h).status_code == 200

    remap = client.post("/admin/robots/remap",
                        json={"namespace": "team/remap_v2",
                              "prewarm_topics": [TOPIC]},
                        headers=admin_headers)
    assert remap.json()["epoch"] == 2

    stale = client.post("/robots/remap/commands", json=_cmd(2), headers=h)
    assert stale.status_code == 401
    assert stale.json()["detail"]["reason"] == "stale_authorization"

    new_tok = _token(client, admin_headers, "remap")
    ok = client.post("/robots/remap/commands", json=_cmd(1),
                     headers={"Authorization": f"Bearer {new_tok}"})
    assert ok.status_code == 200
    assert ok.json()["topic"] == "/team/remap_v2/cmd/move"

    assert "/team/remap/cmd/move" not in gw.bridge.status()["publishers"]


def test_deregistration_revokes_all_access(client, admin_headers):
    _register(client, admin_headers, "doomed", "team/doomed")
    tok = _token(client, admin_headers, "doomed")
    r = client.delete("/admin/robots/doomed", headers=admin_headers)
    assert r.status_code == 200
    after = client.post("/robots/doomed/commands", json=_cmd(1),
                        headers={"Authorization": f"Bearer {tok}"})
    assert after.status_code == 404


# --------------------------------------------------------------------------- #
# 9. Query isolation
# --------------------------------------------------------------------------- #


def test_state_query_is_scoped_to_caller_robot(client, admin_headers):
    tok_a = _token(client, admin_headers, "uni_a")
    resp = client.get("/robots/uni_a/state",
                      headers={"Authorization": f"Bearer {tok_a}"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["robot_id"] == "uni_a"
    assert data["namespace"] == "team/uni_a"
    assert "uni_b" not in json.dumps(data)

    cross = client.get("/robots/uni_b/state",
                       headers={"Authorization": f"Bearer {tok_a}"})
    assert cross.status_code == 401


# --------------------------------------------------------------------------- #
# 10. SSE isolation + immediate stream close on remap + admin global view
# --------------------------------------------------------------------------- #




# --------------------------------------------------------------------------- #
# 10. SSE isolation + immediate stream close on remap + admin global view
#
# SSE is verified over a REAL uvicorn TCP socket (the `server` fixture), not
# the in-process ASGI transport: httpx 0.28's ASGITransport buffers an entire
# response body and therefore cannot observe an infinite stream. A small
# blocking client runs each SSE connection on its own daemon thread and hands
# parsed events back through a thread-safe queue.
# --------------------------------------------------------------------------- #


class _SSEConnection:
    """One blocking SSE connection running on a background thread."""

    def __init__(self, base_url, path, headers):
        # trust_env=False so a sandbox HTTP(S)_PROXY cannot redirect the
        # loopback SSE connection through a SOCKS proxy.
        self._client = httpx.Client(base_url=base_url, trust_env=False,
                                    timeout=httpx.Timeout(
                                        connect=10, read=None, write=10,
                                        pool=10))
        self._queue: "queue.Queue[tuple[str, dict] | None]" = queue.Queue()
        self._path = path
        self._headers = headers
        self._stop = threading.Event()
        self.response_status = None
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            with self._client.stream("GET", self._path,
                                     headers=self._headers) as resp:
                self.response_status = resp.status_code
                if resp.status_code != 200:
                    self._queue.put(("_status",
                                     {"status": resp.status_code}))
                    return
                event_type = "message"
                data_lines: list[str] = []
                for raw in resp.iter_lines():
                    if self._stop.is_set():
                        break
                    if raw.startswith("event:"):
                        event_type = raw.split(":", 1)[1].strip()
                    elif raw.startswith("data:"):
                        data_lines.append(raw.split(":", 1)[1].strip())
                    elif raw == "" and data_lines:
                        try:
                            payload = json.loads("".join(data_lines))
                        except json.JSONDecodeError:
                            payload = {"_raw": "".join(data_lines)}
                        self._queue.put((event_type, payload))
                        event_type, data_lines = "message", []
        except Exception as exc:  # noqa: BLE001
            self._queue.put(("_error", {"error": repr(exc)}))
        finally:
            self._queue.put(None)  # stream ended sentinel

    def next_event(self, timeout=8.0):
        item = self._queue.get(timeout=timeout)
        if item is None:
            raise AssertionError("SSE stream ended before expected event")
        return item

    def expect_end(self, timeout=8.0):
        item = self._queue.get(timeout=timeout)
        assert item is None, f"expected stream end, got {item!r}"

    def stop(self):
        self._stop.set()
        self._client.close()


def test_sse_robot_stream_is_isolated_and_closes_on_remap(
        server, client, admin_headers):
    # Dedicated robots so this test's remap cannot invalidate the shared
    # uni_a/uni_b sessions exercised by other tests.
    _register(client, admin_headers, "sse_a", "team/sse_a")
    _register(client, admin_headers, "sse_b", "team/sse_b")
    tok_a = _token(client, admin_headers, "sse_a")
    tok_b = _token(client, admin_headers, "sse_b")

    sa = _SSEConnection(server, "/robots/sse_a/events",
                        {"Authorization": f"Bearer {tok_a}"})
    sb = _SSEConnection(server, "/robots/sse_b/events",
                        {"Authorization": f"Bearer {tok_b}"})
    try:
        assert sa.next_event()[0] == "ready"
        assert sb.next_event()[0] == "ready"

        # Beta publishes: only the beta stream observes it.
        client.post("/robots/sse_b/commands", json=_cmd(2001),
                    headers={"Authorization": f"Bearer {tok_b}"})
        name, data = sb.next_event()
        assert name == "command_published"
        assert data["rid"] == "sse_b"

        # Remap alpha: its stream terminates immediately with the terminal
        # mapping_reset sentinel.
        client.post("/admin/robots/sse_a",
                    json={"namespace": "team/sse_a_v2"},
                    headers=admin_headers)
        name, data = sa.next_event()
        assert name == "mapping_reset"
        assert data["terminal"] is True
        assert data.get("rid") != "sse_b"
        sa.expect_end()

        # Beta's stream is unaffected by alpha's remap and keeps flowing.
        client.post("/robots/sse_b/commands", json=_cmd(2002),
                    headers={"Authorization": f"Bearer {tok_b}"})
        name, data = sb.next_event()
        assert name == "command_published"
        assert data["rid"] == "sse_b"
    finally:
        sa.stop()
        sb.stop()


def test_sse_admin_sees_all_robots_and_requires_admin(server, client,
                                                      admin_headers):
    # Missing admin token is rejected before the stream opens.
    with httpx.Client(trust_env=False) as plain:
        bad = plain.get(f"{server}/admin/events")
        assert bad.status_code == 401

    admin_stream = _SSEConnection(
        server, "/admin/events", {"X-Admin-Token": ADMIN_TOKEN})
    try:
        assert admin_stream.next_event()[0] == "ready"

        tok = _token(client, admin_headers, "recon")
        client.post("/robots/recon/commands",
                    json=_cmd(3001, payload={"n": 2}),
                    headers={"Authorization": f"Bearer {tok}"})
        name, data = admin_stream.next_event()
        assert name == "command_published"
        assert data["rid"] == "recon"
    finally:
        admin_stream.stop()


def test_sse_requires_valid_robot_token(server):
    with httpx.Client(trust_env=False) as plain:
        r = plain.get(f"{server}/robots/uni_a/events")
        assert r.status_code == 401
