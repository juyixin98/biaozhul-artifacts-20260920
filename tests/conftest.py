"""Shared pytest fixtures.

Unit tests use a temporary SQLite store directly. End-to-end tests spawn the
installed ``segtask_server_node`` as a subprocess on an isolated
``ROS_DOMAIN_ID`` (each test uses a different ID to avoid DDS cross-talk) with
``ROS_LOCALHOST_ONLY=1``.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time

import pytest

from segtask_server.crypto import generate_secret
from segtask_server.db import Store

# Server binary resolution: $SEGTASK_NODE_BIN wins; otherwise the default
# install location of this workspace.
_DEFAULT_ROOT = os.path.join(
    os.path.expanduser('~'), 'Downloads/biaozhul/P044/a')
_NODE_BIN = os.environ.get(
    'SEGTASK_NODE_BIN',
    os.path.join(_DEFAULT_ROOT,
                 'install/segtask_server/lib/segtask_server/segtask_server_node'))
_AUDIT_BIN = os.environ.get(
    'SEGTASK_AUDIT_BIN',
    os.path.join(_DEFAULT_ROOT,
                 'install/segtask_server/lib/segtask_server/segtask_audit'))


def _free_domain_id() -> int:
    # ROS_DOMAIN_ID valid range 0..101; pick a pseudo-random high value.
    import random
    base = random.Random(time.time_ns()).randint(40, 95)
    return base


@pytest.fixture
def secret():
    return generate_secret()


@pytest.fixture
def store(tmp_path, secret):
    db = str(tmp_path / 'test.db')
    s = Store(db, secret, run_id='testrun')
    yield s
    s.close()


class ServerProcess:
    def __init__(self, db_path, domain, mode='resume', drop_rate=0.0,
                 drop_seed=-1, node_bin=_NODE_BIN, log_dir=None):
        self.db_path = db_path
        self.domain = domain
        self.log_path = os.path.join(log_dir or tempfile.gettempdir(),
                                     f'server-{domain}.log')
        cmd = [node_bin, '--ros-args', '-p', f'db_path:={db_path}',
               '-p', f'recovery_mode:={mode}',
               '-p', f'feedback_drop_rate:={drop_rate}',
               '-p', f'feedback_drop_seed:={drop_seed}']
        self.log = open(self.log_path, 'w')
        env = dict(os.environ)
        env['ROS_DOMAIN_ID'] = str(domain)
        env['ROS_LOCALHOST_ONLY'] = '1'
        env['PYTHONUNBUFFERED'] = '1'
        self.proc = subprocess.Popen(cmd, env=env, stdout=self.log,
                                     stderr=subprocess.STDOUT)

    def wait_ready(self, timeout=20.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                self.log.flush()
                raise RuntimeError(
                    f'server exited early (rc={self.proc.returncode}); '
                    f'log: {self.log_path}')
            try:
                with open(self.log_path) as fh:
                    content = fh.read()
                if 'action server ready' in content:
                    return
            except FileNotFoundError:
                pass
            time.sleep(0.1)
        raise RuntimeError(f'server did not become ready; log: {self.log_path}')

    def kill(self):
        if self.proc.poll() is None:
            os.kill(self.proc.pid, signal.SIGKILL)
            self.proc.wait(timeout=10)
        self.log.close()

    def terminate(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.kill()
        self.log.close()


@pytest.fixture
def server_factory(tmp_path):
    created = []

    def _factory(mode='resume', db_name='e2e.db', drop_rate=0.0, drop_seed=-1):
        db_path = str(tmp_path / db_name)
        domain = _free_domain_id()
        sp = ServerProcess(db_path, domain, mode=mode, drop_rate=drop_rate,
                           drop_seed=drop_seed, log_dir=str(tmp_path))
        created.append(sp)
        return sp

    yield _factory
    for sp in created:
        sp.terminate()


@pytest.fixture
def node_env(monkeypatch):
    def _set(domain):
        monkeypatch.setenv('ROS_DOMAIN_ID', str(domain))
        monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    return _set


@pytest.fixture(scope='session')
def node_bin():
    if not os.path.exists(_NODE_BIN):
        pytest.skip(f'server not built: {_NODE_BIN} (run colcon build first)')
    return _NODE_BIN


@pytest.fixture(scope='session')
def audit_bin():
    if not os.path.exists(_AUDIT_BIN):
        pytest.skip(f'audit tool not built: {_AUDIT_BIN}')
    return _AUDIT_BIN
