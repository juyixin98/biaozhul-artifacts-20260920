"""Shared pytest fixtures.

The whole system — gateway rclpy bridge, synthetic robot nodes, FastAPI app —
runs in one pytest process against *real* DDS discovery; no ROS component is
mocked. The bridge uses its own rclpy context, synthetic robots use the
default context, both on an isolated domain id so the suite cannot interfere
with other ROS traffic on the machine.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time

# Isolated DDS domain BEFORE rclpy is imported anywhere; keep traffic local.
os.environ.setdefault("ROS_LOCALHOST_ONLY", "1")
os.environ.setdefault("ROS_DOMAIN_ID", str(20 + (os.getpid() % 80)))

import httpx  # noqa: E402
import pytest  # noqa: E402
import rclpy  # noqa: E402
import socket  # noqa: E402
import uvicorn  # noqa: E402
from rclpy.node import Node  # noqa: E402
from rclpy.executors import SingleThreadedExecutor  # noqa: E402
from rclpy.qos import (  # noqa: E402
    QoSProfile, ReliabilityPolicy, DurabilityPolicy, HistoryPolicy)
from std_msgs.msg import String  # noqa: E402

from mr_gateway.app import create_app  # noqa: E402
from mr_gateway.config import Settings  # noqa: E402

ROBOT_QOS = QoSProfile(
    reliability=ReliabilityPolicy.RELIABLE,
    durability=DurabilityPolicy.TRANSIENT_LOCAL,
    history=HistoryPolicy.KEEP_LAST,
    depth=20,
)

ADMIN_TOKEN = "test-admin-token"
HMAC_SECRET = "test-hmac-secret"
_DDS_USED = False  # set once any rclpy context is initialized in this process


# --------------------------------------------------------------------------- #
# Dedicated event loop shared by the ASGI app and async tests.
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever,
                              name="pytest-async-loop", daemon=True)
    thread.start()
    asyncio.set_event_loop(loop)
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=3)
    loop.close()


def run_coro(loop, coro, timeout=20):
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout)


def pytest_sessionfinish(session, exitstatus):  # noqa: ARG001
    """Record the real pytest outcome for pytest_unconfigure."""
    os.environ["_MRGW_TEST_EXIT"] = str(exitstatus)


def pytest_unconfigure(config):  # noqa: ARG001
    # All explicit rclpy/executor teardown has already run in the fixtures.
    # rmw_fastrtps aborts during *process-exit* C++ static destruction when
    # many DDS participants were created and destroyed in one pytest process
    # ("terminate called without an active exception", exit 134) — this is a
    # post-test global-destructor race, not a test failure. Only when DDS was
    # actually used do we flush and hard-exit with pytest's own status so CI
    # sees the real pass/fail code; pure-unit runs exit normally.
    if not globals().get("_DDS_USED"):
        return
    import sys
    sys.stdout.flush()
    sys.stderr.flush()
    code = int(os.environ.get("_MRGW_TEST_EXIT", "0"))
    if code not in (0,):
        code = 1  # pytest failure (1) / no-tests (5) map to a non-zero code
    os._exit(code)


@pytest.fixture()
def arun(event_loop):
    """Run an async body on the single shared event loop.

    Tests stay synchronous (so they can mix blocking DDS waits with HTTP),
    while every coroutine — ASGI app, SSE streams, event bus — executes on the
    one loop that owns those objects. Cross-loop asyncio.Queue would
    otherwise deadlock, so this is the supported way to await in a test.
    """
    def _run(coro, timeout=20):
        return run_coro(event_loop, coro, timeout=timeout)
    return _run


class _SyncASGITransport(httpx.BaseTransport):
    """Adapt httpx's async ASGITransport to the blocking Client interface.

    Every request is scheduled onto the shared pytest event loop and blocked
    on from the test thread, so sync-style test code still drives the same
    ASGI app (and therefore the same singleton State) as async SSE clients.
    """

    def __init__(self, app, loop):
        self._async = httpx.ASGITransport(app=app)
        self._loop = loop

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        return run_coro(self._loop, self._handle_async(request), timeout=30)

    async def _handle_async(self, request):
        async with httpx.AsyncClient(
                transport=self._async,
                base_url="http://asgi.local") as ac:
            response = await ac.request(
                request.method,
                request.url.raw_path.decode("ascii"),
                headers=dict(request.headers),
                content=request.content,
            )
            await response.aread()
        return httpx.Response(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=response.content,
            request=request,
        )


# --------------------------------------------------------------------------- #
# Gateway under test (real bridge, real ROS context).
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="session")
def app(event_loop, tmp_path_factory):
    settings = Settings(
        hmac_secret=HMAC_SECRET,
        admin_token=ADMIN_TOKEN,
        default_ttl_seconds=3600,
        audit_log_path=str(tmp_path_factory.mktemp("audit") / "gateway.jsonl"),
        host="127.0.0.1",
        port=0,
    )
    application = create_app(settings)
    application.state.gw.bridge.start()
    globals()["_DDS_USED"] = True
    yield application
    application.state.gw.bridge.stop()
    application.state.gw.audit.close()


@pytest.fixture(scope="session")
def gw(app):
    return app.state.gw


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def server(app):
    """Serve the SAME app instance over a real TCP socket with uvicorn.

    Necessary for SSE: httpx 0.28's ASGITransport buffers the whole response
    body (it awaits app completion before returning), which can never finish
    for an infinite stream. A real HTTP connection delivers chunks as they
    are produced.
    """
    config = uvicorn.Config(app, host="127.0.0.1", port=_free_port(),
                            log_level="warning")
    server = uvicorn.Server(config)
    server.config.load()
    thread = threading.Thread(target=server.run, name="uvicorn-test",
                              daemon=True)
    thread.start()
    # Wait until the socket accepts connections.
    deadline = time.time() + 15
    while time.time() < deadline and not server.started:
        time.sleep(0.05)
    yield f"http://127.0.0.1:{config.port}"
    server.should_exit = True
    thread.join(timeout=5)


@pytest.fixture(scope="session")
def client(app, event_loop):
    # httpx 0.28's ASGITransport is async-only; drive it through the shared
    # event loop so the sync Client used by tests talks to the same app
    # instance as the async SSE clients.
    transport = _SyncASGITransport(app, event_loop)
    with httpx.Client(transport=transport, base_url="http://gw.local",
                      timeout=20) as c:
        yield c


@pytest.fixture()
def admin_headers():
    return {"X-Admin-Token": ADMIN_TOKEN}


@pytest.fixture(scope="session", autouse=True)
def _shared_registrations(client):
    """Register a stable set of shared robots once for the whole session.

    Individual tests that need fresh sequence watermarks register their own
    robot ids (iso_*, exp, seq, remap, …). The shared ids are only ever used
    for authorization/topology checks, so cross-test watermarks don't matter.
    """
    hdr = {"X-Admin-Token": ADMIN_TOKEN}
    for rid, ns in (("uni_a", "team/uni_a"), ("uni_b", "team/uni_b"),
                    ("recon", "team/recon")):
        r = client.post(f"/admin/robots/{rid}",
                        json={"namespace": ns,
                              "prewarm_topics": ["cmd/move"]},
                        headers=hdr)
        assert r.status_code == 200, r.text
    yield


# --------------------------------------------------------------------------- #
# Synthetic robots (real ROS subscribers on the default context).
# --------------------------------------------------------------------------- #


class SyntheticRobot:
    """A real rclpy node in one namespace, recording received envelopes.

    Owns its own rclpy context + executor so multiple simulated robots (and
    the gateway) can coexist in one pytest process like separate processes.
    """

    def __init__(self, name: str, namespace: str, topics: list[str]):
        self.namespace = namespace.strip("/")
        self._ctx = rclpy.Context()
        self._ctx.init()
        self.node = Node(name, namespace="/" + self.namespace,
                         context=self._ctx)
        self.messages: list[dict] = []
        self._lock = threading.Lock()
        self._topics = topics
        for topic in topics:
            fqn = "/" + self.namespace + "/" + topic.strip("/")
            self.node.create_subscription(
                String, fqn, self._cb(topic), ROBOT_QOS)
        self._executor = SingleThreadedExecutor(context=self._ctx)
        self._executor.add_node(self.node)
        self._thread = threading.Thread(
            target=self._executor.spin, name=f"robot-{name}", daemon=True)
        self._thread.start()

    def _cb(self, topic: str):
        def cb(msg: String):
            try:
                envelope = json.loads(msg.data)
            except json.JSONDecodeError:
                envelope = {"_raw": msg.data}
            envelope["_recv_topic"] = topic
            with self._lock:
                self.messages.append(envelope)
        return cb

    def received(self) -> list[dict]:
        with self._lock:
            return list(self.messages)

    def stop(self):
        self._executor.shutdown()
        self._thread.join(timeout=3)
        self.node.destroy_node()
        self._ctx.shutdown()


@pytest.fixture()
def robots():
    created: list[SyntheticRobot] = []

    def make(name, namespace, topics=("cmd/move",)):
        robot = SyntheticRobot(name, namespace, list(topics))
        created.append(robot)
        return robot

    yield make
    for robot in created:
        try:
            robot.stop()
        except Exception:  # noqa: BLE001
            pass
    created.clear()


def wait_for_subscribers(gw, fqn, count: int = 1, timeout: float = 8.0):
    """Block until the gateway sees `count` live subscribers on a topic."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if gw.bridge.subscriber_count(fqn) >= count:
            return True
        time.sleep(0.1)
    return False


def wait_for_messages(robot, count: int = 1, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(robot.received()) >= count:
            return robot.received()
        time.sleep(0.1)
    return robot.received()


# --------------------------------------------------------------------------- #
# API helpers
# --------------------------------------------------------------------------- #


def register_robot(client, robot_id, namespace, headers):
    resp = client.post(f"/admin/robots/{robot_id}",
                       json={"namespace": namespace}, headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


def issue(client, robot_id, tester_id="tester-1", headers=None, ttl=None):
    body = {"tester_id": tester_id}
    if ttl is not None:
        body["ttl_seconds"] = ttl
    resp = client.post(f"/admin/robots/{robot_id}/tokens", json=body,
                       headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def auth(token):
    return {"Authorization": f"Bearer {token}"}
