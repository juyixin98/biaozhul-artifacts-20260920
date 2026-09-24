"""pytest 公共夹具: 每个测试使用独立数据目录, 并把应用服务指向它。"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.config import Settings
from app.service import Service

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture
def svc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Service]:
    service = Service(Settings(tmp_path / "data"))
    monkeypatch.setattr(main_mod, "service", service)
    yield service


@pytest.fixture
def client(svc: Service) -> TestClient:
    return TestClient(main_mod.app)


def _example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


@pytest.fixture
def registered(svc: Service) -> dict[str, str]:
    """注册 bag/params/cal(v1,v2)/algo 并创建快照 S1(v1标定)、S2(v2标定)。"""
    bag = svc.register_bag((EXAMPLES / "bag.json").read_bytes())
    params = svc.register_params(_example("params.json"))
    cals = _example("calibrations.json")
    algos = _example("algorithms.json")
    c1 = svc.register_calibration(cals["v1"])
    c2 = svc.register_calibration(cals["v2"])
    a1 = svc.register_algorithm(algos["v1"])
    refs = {
        "bag_id": bag["id"],
        "params_id": params["id"],
        "calibration_id": c1["id"],
        "algorithm_id": a1["id"],
    }
    s1 = svc.create_snapshot(refs)
    s2 = svc.create_snapshot({**refs, "calibration_id": c2["id"]})
    return {"s1": s1["id"], "s2": s2["id"], "bag": bag["id"]}


def wait_attempt(svc: Service, attempt_id: str, tries: int = 100) -> dict:
    import time

    for _ in range(tries):
        a = svc.get_attempt(attempt_id)
        if a["status"] != "running":
            return a
        time.sleep(0.03)
    raise AssertionError(f"attempt {attempt_id} 一直处于 running")
