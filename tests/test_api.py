"""End-to-end HTTP API tests via FastAPI's TestClient."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.conftest import requires_ros


@pytest.fixture()
def client(tmp_path, monkeypatch):
    bag_root = tmp_path / "bags"
    bag_root.mkdir()
    state = tmp_path / "state"
    state.mkdir()

    # Build settings directly (no env/reload, so exception class identity is
    # preserved across the test session).
    from app.config import Settings
    from app.main import create_app

    settings = Settings(
        bag_roots=[bag_root],
        state_dir=state,
        secret_file=state / "hmac.key",
        host="127.0.0.1",
        port=8000,
        tick_seconds=0.002,
        transport="loopback",
        control_topic="/replay/control",
    )
    application = create_app(settings)

    from fastapi.testclient import TestClient

    with TestClient(application) as c:
        yield c, bag_root, state


@requires_ros
def test_health(client):
    c, _, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["rosbag2_py"] is True


@requires_ros
def test_create_play_seek_stream_full_flow(client):
    c, bag_root, _ = client
    from bagtools.bagmaker import make_bag

    bag = make_bag(bag_root / "api", messages=24, gap_ns=20_000_000)

    r = c.post("/sessions", json={"bag_uri": bag["uri"], "rate": 50})
    assert r.status_code == 201, r.text
    session = r.json()
    sid = session["session_id"]
    n = session["bag"]["message_count"]
    assert n == 24

    import time

    r = c.post(f"/sessions/{sid}/play")
    assert r.status_code == 200
    assert r.json()["state"]["playing"] is True
    deadline = time.time() + 5
    while time.time() < deadline:
        if c.get(f"/sessions/{sid}").json()["state"]["published_count"] >= n:
            break
        time.sleep(0.02)

    # seek to seq 20 -> new generation, play
    r = c.post(f"/sessions/{sid}/seek", json={"seq": 20, "play_after": True})
    assert r.status_code == 200, r.text
    assert r.json()["state"]["generation"] >= 1

    # seek by ratio to the end
    r = c.post(f"/sessions/{sid}/seek", json={"ratio": 1.0})
    assert r.status_code == 200
    assert r.json()["state"]["next_seq"] == n + 1

    # jump back to start and let the new generation finish
    r = c.post(f"/sessions/{sid}/seek", json={"seq": 1, "play_after": True})
    assert r.status_code == 200
    last_gen = r.json()["state"]["generation"]
    st = None
    deadline = time.time() + 5
    while time.time() < deadline:
        st = c.get(f"/sessions/{sid}").json()["state"]
        if st["finished"]:
            break
        time.sleep(0.02)
    assert st and st["finished"]

    all_items = c.get(
        f"/sessions/{sid}/messages", params={"limit": 5000}
    ).json()["items"]
    msgs = [x for x in all_items if x.get("kind") == "message"]
    assert msgs
    last_gen_msgs = [m for m in msgs if m["generation"] == last_gen]
    assert [m["seq"] for m in last_gen_msgs] == list(range(1, n + 1))
    gens = sorted({m["generation"] for m in msgs})
    assert gens == sorted(set(gens))  # monotonic; an end-seek gen may have 0 msgs
    assert 0 in gens and last_gen in gens

    r = c.delete(f"/sessions/{sid}")
    assert r.status_code == 204


@requires_ros
def test_ndjson_stream_delivers_envelopes(tmp_path):
    """The NDJSON stream emits live envelopes over a REAL HTTP server.

    FastAPI's TestClient runs on a single blocking portal that cannot run a
    long-lived streaming response concurrently with client calls, so this
    test spawns uvicorn on an ephemeral port -- the same path an operator
    uses locally.
    """
    import json as _json
    import socket
    import subprocess
    import sys
    import threading
    import time
    import urllib.request

    from bagtools.bagmaker import make_bag

    bag_root = tmp_path / "bags"
    bag_root.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    bag = make_bag(bag_root / "stream", messages=8, gap_ns=100_000_000)

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    env = dict(os.environ)
    env.update(
        REPLAY_BAG_ROOTS=str(bag_root),
        REPLAY_STATE_DIR=str(state),
        REPLAY_HMAC_KEY="stream-test-secret-key-0123456789",
        REPLAY_TRANSPORT="loopback",
    )
    # Ensure the loopback connection bypasses any ambient SOCKS/HTTP proxy.
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                opener.open(base + "/health", timeout=1)
                break
            except Exception:
                time.sleep(0.2)
        else:
            raise AssertionError("server did not start")

        req = urllib.request.Request(
            base + "/sessions",
            data=_json.dumps({"bag_uri": bag["uri"], "rate": 1.0}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        sid = _json.load(opener.open(req))["session_id"]

        got: list[dict] = []

        def consume():
            with opener.open(f"{base}/sessions/{sid}/stream", timeout=10) as resp:
                while True:
                    line = resp.readline()
                    if not line:
                        break
                    got.append(_json.loads(line))
                    if len([g for g in got if g.get("kind") == "message"]) >= 4:
                        return

        t = threading.Thread(target=consume, daemon=True)
        t.start()
        time.sleep(0.5)  # subscription established
        opener.open(
            urllib.request.Request(f"{base}/sessions/{sid}/play", method="POST")
        )
        t.join(timeout=8)

        assert any(g.get("kind") == "transport" for g in got)
        assert len([g for g in got if g.get("kind") == "message"]) >= 4
        first = next(g for g in got if g.get("kind") == "message")
        assert first["timestamp_ns"] >= 1_000_000_000
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


@requires_ros
def test_seek_rejects_out_of_range(client):
    c, bag_root, _ = client
    from bagtools.bagmaker import make_bag

    bag = make_bag(bag_root / "range", messages=5)
    sid = c.post("/sessions", json={"bag_uri": bag["uri"]}).json()["session_id"]
    r = c.post(f"/sessions/{sid}/seek", json={"seq": 999})
    assert r.status_code == 400
    r = c.post(f"/sessions/{sid}/seek", json={"seq": 1, "timestamp_ns": 123})
    assert r.status_code == 422  # pydantic model validation (two targets)


@requires_ros
def test_corrupt_bag_create_returns_422(client):
    c, bag_root, _ = client
    from bagtools.bagmaker import make_corrupt

    make_corrupt(bag_root / "bad", "garbage_storage", messages=8)
    r = c.post("/sessions", json={"bag_uri": str(bag_root / "bad")})
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "corrupt_bag"


@requires_ros
def test_checkpoint_roundtrip_via_http_source_changed_409(client):
    c, bag_root, _ = client
    from bagtools.bagmaker import make_bag

    bag = make_bag(bag_root / "httpcp", messages=14, gap_ns=20_000_000)
    sid = c.post(
        "/sessions", json={"bag_uri": bag["uri"], "rate": 50, "autoplay": True}
    ).json()["session_id"]

    import time
    deadline = time.time() + 5
    while c.get(f"/sessions/{sid}").json()["state"]["next_seq"] < 7:
        time.sleep(0.02)
    r = c.post(f"/sessions/{sid}/checkpoint", json={"pause": True})
    assert r.status_code == 200
    cp_id = r.json()["checkpoint_id"]
    cp_next = r.json()["state"]["next_seq"]

    # mutate the bag on disk
    mcap = next(Path(bag["uri"]).glob("*.mcap"))
    with mcap.open("ab") as fh:
        fh.write(b"\x00\x00")

    r = c.post("/restore", json={"checkpoint_id": cp_id})
    assert r.status_code == 409
    assert r.json()["detail"]["error"] == "source_changed"


@requires_ros
def test_checkpoint_restart_continues_via_http(client):
    c, bag_root, _ = client
    from bagtools.bagmaker import make_bag

    bag = make_bag(bag_root / "cont", messages=18, gap_ns=20_000_000)
    sid = c.post(
        "/sessions", json={"bag_uri": bag["uri"], "rate": 50, "autoplay": True}
    ).json()["session_id"]

    import time
    deadline = time.time() + 5
    while c.get(f"/sessions/{sid}").json()["state"]["next_seq"] < 10:
        time.sleep(0.02)
    cp_id = c.post(
        f"/sessions/{sid}/checkpoint", json={"pause": True}
    ).json()["checkpoint_id"]
    cp_next = c.get(f"/sessions/{sid}").json()["state"]["next_seq"]

    r = c.post("/restore", json={"checkpoint_id": cp_id, "autoplay": True})
    assert r.status_code == 200, r.text
    new_sid = r.json()["session_id"]
    assert r.json()["state"]["next_seq"] == cp_next

    deadline = time.time() + 5
    while time.time() < deadline:
        st = c.get(f"/sessions/{new_sid}").json()["state"]
        if st["finished"]:
            break
        time.sleep(0.02)
    assert st["finished"]

    gen = st["generation"]
    msgs = c.get(f"/sessions/{new_sid}/messages").json()["items"]
    msgs = [m for m in msgs if m.get("kind") == "message" and m["generation"] == gen]
    assert [m["seq"] for m in msgs] == list(range(cp_next, 19))


@requires_ros
def test_filter_validation(client):
    c, bag_root, _ = client
    from bagtools.bagmaker import make_bag

    bag = make_bag(bag_root / "f", messages=6)
    sid = c.post("/sessions", json={"bag_uri": bag["uri"]}).json()["session_id"]
    r = c.post(f"/sessions/{sid}/filter", json={"topics": ["/nope"]})
    assert r.status_code == 400
    r = c.post(f"/sessions/{sid}/filter", json={"topics": ["/alpha"]})
    assert r.status_code == 200
    assert r.json()["state"]["topic_filter"] == ["/alpha"]
