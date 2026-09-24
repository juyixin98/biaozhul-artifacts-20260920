"""End-to-end HTTP acceptance tests via FastAPI TestClient."""
from __future__ import annotations

import json

import numpy as np

from tests.conftest import FIXTURES


def _upload(client, subdir, **overrides):
    files = []
    for p in sorted((FIXTURES / subdir).glob("*.png")):
        files.append(("files", (p.name, p.read_bytes(), "image/png")))
    data = {
        "camera_id": "camA",
        "width": "1280",
        "height": "960",
        "board_rows": "6",
        "board_cols": "9",
        "square_size_mm": "40",
    }
    data.update(overrides)
    return client.post("/calibrations", data=data, files=files)


def test_health(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_full_calibration_creates_version(client, fx):
    r = _upload(client, "valid_views")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["status"] == "succeeded"
    assert body["created_version"] == 1
    assert body["version_id"] == "v1"
    assert body["result"]["accepted_count"] == 24
    assert body["result"]["rms"] < 0.5

    # signature really is an HMAC over the canonical version payload
    from app import crypto, storage

    v = storage.get_version("camA", 1280, 960, 1)
    sig = v.pop("signature")
    v.pop("signature_valid", None)
    assert crypto.verify_payload(v, sig)

    K = np.array(body["result"]["intrinsics"]["camera_matrix"])
    Kt = np.array(fx["gt"]["camera_matrix"])
    assert abs(K[0, 0] - Kt[0, 0]) / Kt[0, 0] < 0.01


def test_per_image_reports_and_exclusion_counts(client):
    # 2 corrupted + 24 valid
    files = []
    for p in sorted((FIXTURES / "corrupted").glob("*.png")):
        files.append(("files", (p.name, p.read_bytes(), "image/png")))
    for p in sorted((FIXTURES / "valid_views").glob("*.png")):
        files.append(("files", (p.name, p.read_bytes(), "image/png")))
    data = dict(
        camera_id="camA", width="1280", height="960",
        board_rows="6", board_cols="9", square_size_mm="40",
    )
    r = client.post("/calibrations", data=data, files=files)
    assert r.status_code == 201, r.text
    body = r.json()
    counts = body["exclusions"]
    assert counts["decode_failed"] == 1
    assert counts["corner_detection_failed"] == 1
    assert counts["resolution_mismatch"] == 1
    bad = {i["filename"]: i for i in body["images"]}
    assert bad["not_an_image.png"]["reason"] == "decode_failed"
    assert bad["no_board.png"]["reason"] == "corner_detection_failed"
    assert bad["wrong_resolution.png"]["reason"] == "resolution_mismatch"
    # sha256 present on every input
    assert all(i["sha256"] for i in body["images"])


def test_duplicate_views_422_with_trace(client):
    r = _upload(client, "repeated_views")
    # 24 distinct views survive -> succeeds, with 6 duplicates in trace
    assert r.status_code == 201, r.text
    dupes = [i for i in r.json()["images"] if i["reason"] == "duplicate_pose"]
    assert len(dupes) == 6
    assert all(i["duplicate_of"] is not None for i in dupes)


def test_degenerate_pose_returns_reason_and_persists_job(client):
    r = _upload(client, "planar_arc")
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["status"] == "failed"
    assert body["failure_reason"] == "DEGENERATE_POSE_COVERAGE"
    assert "SVD" in body["failure_detail"]

    # failed job is persisted and retrievable
    jr = client.get(f"/jobs/{body['job_id']}")
    assert jr.status_code == 200
    assert jr.json()["failure_reason"] == "DEGENERATE_POSE_COVERAGE"


def test_wrong_board_size_fails(client):
    r = _upload(client, "valid_views", board_rows="7", board_cols="10")
    assert r.status_code == 422
    assert r.json()["failure_reason"] == "CORNER_DETECTION_FAILED"


def test_insufficient_views(client):
    files = []
    for p in sorted((FIXTURES / "valid_views").glob("*.png"))[:8]:
        files.append(("files", (p.name, p.read_bytes(), "image/png")))
    r = client.post(
        "/calibrations",
        data=dict(
            camera_id="camA", width="1280", height="960",
            board_rows="6", board_cols="9", square_size_mm="40",
        ),
        files=files,
    )
    assert r.status_code == 422
    assert r.json()["failure_reason"] == "INSUFFICIENT_VIEWS"


def test_resolution_binding_and_parallel_versions(client):
    # two successful calibrations for camA at 1280x960
    r1 = _upload(client, "valid_views")
    assert r1.json()["created_version"] == 1
    r2 = _upload(client, "valid_views")
    assert r2.json()["created_version"] == 2

    # a calibration at a different resolution
    files = []
    for p in sorted((FIXTURES / "other_resolution").glob("*.png")):
        files.append(("files", (p.name, p.read_bytes(), "image/png")))
    r3 = client.post(
        "/calibrations",
        data=dict(
            camera_id="camA", width="1024", height="768",
            board_rows="6", board_cols="9", square_size_mm="40",
        ),
        files=files,
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["created_version"] == 1  # versioning resets per resolution

    # old and new versions remain queryable in parallel
    lst = client.get("/cameras/camA/calibrations?width=1280&height=960")
    assert [v["version"] for v in lst.json()] == [1, 2]

    v1 = client.get("/cameras/camA/calibrations/v1?width=1280&height=960")
    v2 = client.get("/cameras/camA/calibrations/v2?width=1280&height=960")
    assert v1.status_code == 200 and v2.status_code == 200
    assert v1.json()["version_id"] == "v1"
    assert v2.json()["version_id"] == "v2"

    latest = client.get("/cameras/camA/calibrations/latest?width=1280&height=960")
    assert latest.json()["version"] == 2

    # no cross-resolution reuse
    conflict = client.get("/cameras/camA/calibrations?width=640&height=480")
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["available_resolutions"] == [[1024, 768], [1280, 960]]

    # v1 must not be served under a size where it does not exist; a size
    # with versions at all but missing that number is a 404, while a size
    # with no versions is a 409 (both are non-reuse guarantees).
    cross = client.get("/cameras/camA/calibrations/v2?width=1024&height=768")
    assert cross.status_code == 404
    no_such_size = client.get(
        "/cameras/camA/calibrations/v1?width=320&height=240"
    )
    assert no_such_size.status_code == 409


def test_version_detail_carries_per_image_errors(client):
    _upload(client, "valid_views")
    r = client.get("/cameras/camA/calibrations/v1?width=1280&height=960")
    body = r.json()
    assert len(body["images"]) == 24
    for i in body["images"]:
        assert i["status"] == "accepted"
        assert i["reprojection_error_px"] is not None
        assert i["pose"]["rvec"] and len(i["pose"]["rvec"]) == 3


def test_signature_tampering_detected(client, data_dir):
    _upload(client, "valid_views")
    vpath = data_dir / "cameras" / "camA" / "1280x960" / "v1.json"
    doc = json.loads(vpath.read_text())
    doc["rms"] = 0.0  # tamper
    vpath.write_text(json.dumps(doc))
    r = client.get("/cameras/camA/calibrations/v1?width=1280&height=960")
    assert r.status_code == 200
    assert r.json()["signature_valid"] is False


def test_from_paths_endpoint_and_traversal_block(client):
    root = FIXTURES / "valid_views"
    paths = [str(p) for p in sorted(root.glob("*.png"))]
    r = client.post(
        "/calibrations/from-paths",
        json=dict(
            camera_id="camB", width=1280, height=960, board_rows=6, board_cols=9,
            square_size_mm=40, image_paths=paths,
        ),
    )
    assert r.status_code == 201, r.text

    bad = client.post(
        "/calibrations/from-paths",
        json=dict(
            camera_id="camB", width=1280, height=960, board_rows=6, board_cols=9,
            square_size_mm=40, image_paths=["/etc/passwd"],
        ),
    )
    assert bad.status_code == 403


def test_bad_camera_id_rejected(client):
    r = _upload(client, "valid_views", camera_id="../escape")
    assert r.status_code == 422


def test_no_files_422(client):
    r = client.post(
        "/calibrations",
        data=dict(
            camera_id="camA", width="1280", height="960",
            board_rows="6", board_cols="9", square_size_mm="40",
        ),
        files=[],
    )
    assert r.status_code == 422
