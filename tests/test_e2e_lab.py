"""End-to-end test over a REAL uvicorn HTTP server and real sockets.

A uvicorn server is started on an ephemeral localhost port in a background
thread. The probe makes genuine HTTP requests; the uplink delay is slept on
the client before POSTing and the downlink delay is slept on the server after
stamping c_send, so both directions are genuinely inside the measured RTT.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import httpx
import pytest
import uvicorn

from scripts.probe import probe


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server(uvicorn.Server):
    def install_signal_handlers(self):  # no signal handling in worker thread
        pass


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    import os
    tmp = tmp_path_factory.mktemp("e2e")
    os.environ["CLOCKDRIFT_DB"] = str(tmp / "e2e.db")
    os.environ["CLOCKDRIFT_KEY_FILE"] = str(tmp / "key")
    # Force a fresh in-process app/store regardless of earlier test modules.
    import app.main as main
    if main._store is not None:
        main._store.close()
    main._store = None
    main._service = None
    import app.lab as lab
    lab._REG.clear()
    port = _free_port()
    config = uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port,
        log_level="warning", lifespan="on",
    )
    srv = _Server(config)
    thread = threading.Thread(target=srv.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with httpx.Client(trust_env=False) as ready:
                r = ready.get(f"{base}/health", timeout=0.5)
                if r.status_code == 200:
                    break
        except Exception:
            time.sleep(0.1)
    else:
        srv.should_exit = True
        thread.join(timeout=5)
        raise RuntimeError("test server did not become ready")
    yield base
    srv.should_exit = True
    thread.join(timeout=5)


@pytest.mark.parametrize("asym", [-0.8, 0.8])
def test_real_roundtrips_asymmetric(server, asym):
    base = server
    with httpx.Client(trust_env=False) as c:
        r = c.post(f"{base}/lab/devices", json={"device_id": f"e2e-{asym}",
                                                "ppm": 130.0})
        assert r.status_code == 201
    # Real sleeps: ~3ms one-way injected; ~14 * (RTT + 30ms pace) ~ 0.6s.
    samples = probe(base, f"e2e-{asym}", n=14, base_delay=0.003,
                    asym=asym, jitter=0.2, processing=0.0)
    with httpx.Client(trust_env=False) as c:
        body = {
            "device_id": f"e2e-{asym}",
            "counter_nominal_hz": 1_000_000.0,
            "samples": samples,
        }
        r = c.post(f"{base}/api/v1/devices/e2e-{asym}/samples?publish=true",
                   json=body, timeout=10)
        assert r.status_code == 200, r.text
        resp = r.json()
    assert resp["status"] == "calibrated", resp
    assert resp["published"] is True
    seg = resp["segments"][-1]
    true_slope = 1.0 / ((1.0 + 130e-6) * 1_000_000.0)
    assert abs(seg["slope"] - true_slope) / true_slope < 5e-3
    iv = seg["offset_interval"]
    assert iv["upper"] > iv["lower"]  # asymmetric network -> real interval


def test_reboot_over_real_network(server):
    base = server
    with httpx.Client(trust_env=False) as c:
        c.post(f"{base}/lab/devices", json={"device_id": "e2e-reboot",
                                            "ppm": 90.0})
    s1 = probe(base, "e2e-reboot", n=8, base_delay=0.002, asym=0.2,
               jitter=0.2, processing=0.0)
    with httpx.Client(trust_env=False) as c:
        assert c.post(f"{base}/lab/devices/e2e-reboot/reboot").status_code == 200
    s2 = probe(base, "e2e-reboot", n=8, base_delay=0.002, asym=0.2,
               jitter=0.2, processing=0.0)
    with httpx.Client(trust_env=False) as c:
        r = c.post(f"{base}/api/v1/devices/e2e-reboot/samples", json={
            "device_id": "e2e-reboot",
            "counter_nominal_hz": 1_000_000.0,
            "samples": s1 + s2,
        })
        assert r.status_code == 200
        resp = r.json()
    kinds = {d["type"] for d in resp["discontinuities"]}
    assert "reboot" in kinds
    assert len(resp["segments"]) >= 2
