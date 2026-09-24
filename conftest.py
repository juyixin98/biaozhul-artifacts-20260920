"""Shared pytest fixtures."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

# Make the package importable when pytest is run from the repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import app  # noqa: E402

CALIBRATION = {
    "frame_id": "base_link",
    "transform": [
        [1.0, 0.0, 0.0, 0.05],
        [0.0, 1.0, 0.0, -0.02],
        [0.0, 0.0, 1.0, 0.10],
        [0.0, 0.0, 0.0, 1.0],
    ],
    "marker_points": [[0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
    "reprojection_error": 0.0012,
}

CALIBRATION_V2 = {
    **CALIBRATION,
    "transform": [
        [1.0, 0.0, 0.0, 0.09],  # translation changed after the job was frozen
        [0.0, 1.0, 0.0, -0.02],
        [0.0, 0.0, 1.0, 0.10],
        [0.0, 0.0, 0.0, 1.0],
    ],
}

PARAMS = {"gain": 1.5, "offset": 0.01, "filter_width": 2.0, "max_range_m": 10.0}
BAG = (
    b"# synthetic scan\n"
    b"0.100,0.200,1.000,1.0\n"
    b"0.150,0.210,1.020,1.0\n"
    b"0.120,0.195,0.980,0.8\n"
    b"0.300,0.400,1.100,1.0\n"
    b"0.310,0.420,1.120,1.0\n"
)
BAG_SUMMARY = {
    "topic": "/scan_front_3d",
    "message_count": 5,
    "duration_sec": 0.15,
    "start_time": "2026-09-23T08:00:00Z",
}


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("SNAPSHOT_ROOT", str(tmp_path / "data"))
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def snapshot_id(client) -> str:
    """A fully built, signed snapshot ready to run jobs against."""
    calib_id = client.post("/calibrations", json=CALIBRATION).json()["id"]
    r = client.post(
        "/bags",
        files={"file": ("bag.csv", BAG, "text/csv")},
        data={"summary": json.dumps(BAG_SUMMARY)},
    )
    bag_digest = r.json()["digest"]
    r = client.post(
        "/snapshots",
        json={
            "bag_digest": bag_digest,
            "params": PARAMS,
            "calibration_id": calib_id,
            "algorithm_name": "pointcloud-transform",
            "algorithm_version": "3.2.1",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]
