"""Shared pytest fixtures: fresh two-chain environment per test.

Each test gets its OWN ephemeral Anvil pair (unique ports derived from the
worker pid) so tests are independent and no state leaks between them.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from htlclib import chains as chains_mod
from htlclib.harness import Env, build_env


def _free_port(preferred: int) -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", preferred))
            return preferred
        except OSError:
            s.bind(("127.0.0.1", 0))
            return s.getsockname()[1]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    port_a = _free_port(8545)
    port_b = _free_port(8546)
    monkeypatch.setattr(chains_mod, "CHAIN_A_RPC", f"http://127.0.0.1:{port_a}")
    monkeypatch.setattr(chains_mod, "CHAIN_B_RPC", f"http://127.0.0.1:{port_b}")
    monkeypatch.setattr(chains_mod, "CHAIN_A_ID", 31337)
    monkeypatch.setattr(chains_mod, "CHAIN_B_ID", 31338)
    monkeypatch.setenv("HTLC_DEPLOYMENT_FILE", str(tmp_path / "deployment.json"))

    e = build_env(log_dir=tmp_path / "logs",
                  deployment_file=tmp_path / "deployment.json",
                  ttl_a=60, delta=20)
    # build_env used chains_mod constants at import time? they are read inside
    # spawn_both -> spawn_chain arguments, so patch is effective.
    yield e
    e.stop()
