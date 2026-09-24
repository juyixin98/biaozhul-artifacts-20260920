"""Integration fixtures: real gateway (uvicorn thread) + two real DDS robots."""

from __future__ import annotations

import json
import os
import socket
import threading
import time

import httpx
import pytest

# An isolated DDS domain so tests cannot interfere with other ROS traffic.
os.environ.setdefault("ROS_DOMAIN_ID", "42")
os.environ.setdefault("RCUTILS_LOGGING_BUFFERED_STREAM", "1")
# Loopback HTTP must never traverse a corporate/SOCKS proxy.
os.environ["NO_PROXY"] = "127.0.0.1,localhost," + os.environ.get("NO_PROXY", "")
os.environ["no_proxy"] = os.environ["NO_PROXY"]

EPOCH_KEY = "ioksSseGEHLK3_AT1mQIr2bI5w2gAv-EwTSdm2g7ZPY"
TALPHA_KEY = "6SkRyx5Fb07IwBAyLkR0y1pSajsbWoG0avrbibiQNP4"
TBRAVO_KEY = "-jy51chNDGv8X56cp3X7xYKqXz6LMpD1rdr9p1ECn8A"
TGUEST_KEY = "EGOu6jhPZPbISDgD6efwPiX_l2q_MivLMenRswwt0J4"
ADMIN_KEY = "integration-admin-key-0123456789abcdef"


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _status_online(client, base_url, robot_id, tester, key) -> bool:
    from p48_gateway.client import signed_request

    try:
        resp = signed_request(
            client, "GET", f"{base_url}/robots/{robot_id}/status",
            tester=tester, key=key,
        )
    except httpx.TransportError:
        return False
    return resp.status_code == 200 and resp.json().get("online") is True


@pytest.fixture(scope="session")
def live_stack(tmp_path_factory):
    pytest.importorskip("rclpy")
    import rclpy

    reg_dir = tmp_path_factory.mktemp("registry")
    registry_path = reg_dir / "registry.json"
    registry_path.write_text(json.dumps({
        "admin_key": ADMIN_KEY,
        "default_ttl_seconds": 5.0,
        "epoch_key": EPOCH_KEY,
        "testers": {
            "tester_alpha": {"key": TALPHA_KEY, "robots": ["alpha", "bravo"]},
            "tester_bravo": {"key": TBRAVO_KEY, "robots": ["bravo"]},
            "tester_guest": {"key": TGUEST_KEY, "robots": []},
        },
        "robots": {
            "alpha": {"namespace": "/p48/alpha"},
            "bravo": {"namespace": "/p48/bravo"},
        },
    }))

    # Imported lazily so environments without a sourced ROS can run unit tests.
    from p48_gateway.api import create_app
    from p48_gateway.gateway_runtime import GatewayRuntime
    from p48_gateway.registry import load_registry
    from p48_gateway.robot_runtime import SyntheticRobot
    import uvicorn

    runtime = GatewayRuntime(str(registry_path))
    port = _free_port()
    config = uvicorn.Config(
        create_app(runtime), host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    thread.start()

    registry = load_registry(registry_path)
    alpha = SyntheticRobot(
        robot_id="alpha", namespace="/p48/alpha",
        epoch_key=EPOCH_KEY, testers=registry.testers, heartbeat_seconds=0.2,
    )
    bravo = SyntheticRobot(
        robot_id="bravo", namespace="/p48/bravo",
        epoch_key=EPOCH_KEY, testers=registry.testers, heartbeat_seconds=0.2,
    )

    base_url = f"http://127.0.0.1:{port}"
    client = httpx.Client(base_url=base_url, timeout=10.0, trust_env=False)

    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        try:
            r = client.get("/healthz")
            if r.status_code == 200 and r.json()["epoch"] == 1:
                if alpha.current_epoch == 1 and bravo.current_epoch == 1:
                    if _status_online(client, base_url, "alpha",
                                      "tester_alpha", TALPHA_KEY) and _status_online(
                            client, base_url, "bravo", "tester_bravo", TBRAVO_KEY):
                        ready = True
                        break
        except httpx.TransportError:
            pass
        time.sleep(0.2)
    if not ready:
        raise RuntimeError("integration stack did not become ready")

    try:
        yield {
            "client": client,
            "base_url": base_url,
            "registry_path": registry_path,
            "runtime": runtime,
            "alpha": alpha,
            "bravo": bravo,
            "keys": {
                "tester_alpha": TALPHA_KEY,
                "tester_bravo": TBRAVO_KEY,
                "tester_guest": TGUEST_KEY,
            },
            "admin_key": ADMIN_KEY,
        }
    finally:
        client.close()
        alpha.stop()
        bravo.stop()
        server.should_exit = True
        runtime.shutdown()
        if rclpy.ok():
            rclpy.shutdown()
        thread.join(timeout=3)
