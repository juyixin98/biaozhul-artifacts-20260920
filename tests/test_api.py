"""HTTP-level tests via FastAPI TestClient (no network, no real server)."""

from __future__ import annotations

import numpy as np


def _payload(x: np.ndarray, *, model_id: str = "tiny-matmul-demo",
             model_version="2026-09-28-v1", request_id="req-test-1"):
    return {
        "model_id": model_id,
        "model_version": model_version,
        "shape": list(x.shape),
        "values": x.reshape(-1).tolist(),
        "request_id": request_id,
    }


def test_health(client) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_infer_accepted_with_verification_metadata(client, fixed_batch) -> None:
    r = client.post("/infer", json=_payload(fixed_batch))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["request_id"] == "req-test-1"
    assert body["verdict"] in {"ACCEPTED", "UNDETERMINED"}
    assert body["calibration_fingerprint"]
    assert len(body["layers"]) == 2
    for layer in body["layers"]:
        for row in layer["output_codes"]:
            assert all(-128 <= c <= 127 for c in row)
        assert layer["saturated_outputs"] >= 0
    # Same request id must appear in the per-layer verification block.
    assert body["verification"]["request_id"] == "req-test-1"


def test_repeated_requests_never_change_calibration(client, fixed_batch) -> None:
    """Two identical requests return byte-identical codes/fingerprint."""
    p1 = client.post("/infer", json=_payload(fixed_batch, request_id="a")).json()
    p2 = client.post("/infer", json=_payload(fixed_batch, request_id="b")).json()
    assert p1["calibration_fingerprint"] == p2["calibration_fingerprint"]
    assert p1["layers"][0]["output_codes"] == p2["layers"][0]["output_codes"]


def test_wrong_version_is_rejected_with_failure_class(client, fixed_batch) -> None:
    payload = _payload(fixed_batch, model_version="9999-not-deployed")
    r = client.post("/infer", json=payload)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "REJECTED_VERSION_MISMATCH"
    assert err["verdict"] == "REJECTED"
    assert err["request_id"] == "req-test-1"
    # Details explain the decision and name the deployed binding.
    assert err["details"]["requested_version"] == "9999-not-deployed"
    assert err["details"]["deployed_version"] == "2026-09-28-v1"
    assert len(err["details"]["calibration_fingerprint"]) == 16


def test_unknown_model_is_rejected(client, fixed_batch) -> None:
    payload = _payload(fixed_batch, model_id="does-not-exist")
    r = client.post("/infer", json=payload)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REJECTED_INVALID_INPUT"
    assert "tiny-matmul-demo" in err["details"]["known_models"]
    # Redaction: caller payload values must never appear in the error body.
    assert "0.5" not in str(err)


def test_shape_mismatch_is_rejected_with_request_id(client, fixed_batch) -> None:
    payload = _payload(fixed_batch)
    payload["shape"] = [2, 4]  # 8 declared vs 12 supplied
    r = client.post("/infer", json=payload)
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REJECTED_INVALID_INPUT"
    assert err["details"]["declared_shape"] == [2, 4]
    assert err["details"]["n_values"] == 12


def test_feature_dimension_mismatch_is_rejected(client) -> None:
    x = np.zeros((1, 3), np.float32)  # model expects 4 features
    r = client.post("/infer", json=_payload(x))
    assert r.status_code == 422
    assert r.json()["error"]["details"]["expected_features"] == 4


def test_non_finite_input_is_rejected(client) -> None:
    # Send NaN as a raw JSON literal: the strict client encoder rejects it,
    # but the server parser accepts the token and must then reject it itself.
    body = (
        '{"model_id": "tiny-matmul-demo", "model_version": "2026-09-28-v1",'
        ' "shape": [1, 4], "values": [1.0, NaN, 0.0, 1.0],'
        ' "request_id": "req-nan"}'
    )
    r = client.post(
        "/infer", content=body, headers={"content-type": "application/json"}
    )
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "REJECTED_INVALID_INPUT"
    assert err["request_id"] == "req-nan"


def test_request_id_is_generated_when_absent(client, fixed_batch) -> None:
    payload = _payload(fixed_batch)
    payload.pop("request_id")
    r = client.post("/infer", json=payload)
    assert r.status_code == 200
    assert r.json()["request_id"].startswith("req-")


def test_model_info_exposes_bound_version_and_fingerprint(client) -> None:
    r = client.get("/models/tiny-matmul-demo")
    assert r.status_code == 200
    info = r.json()
    assert info["model_version"] == "2026-09-28-v1"
    assert info["layer_order"] == ["fc1", "fc2"]
    assert len(info["calibration_fingerprint"]) == 16


def test_core_integer_rejection_is_an_integrity_failure(client, fixed_batch, monkeypatch) -> None:
    """If independent verification ever returns REJECTED, service answers 500."""
    from app import main as main_module
    from app.verification import VerificationReport

    def fake_verify_trace(*args, **kwargs):
        return VerificationReport(
            verdict="REJECTED",
            request_id=kwargs.get("request_id", "?"),
            reasons=["simulated integer/oracle disagreement"],
        )

    monkeypatch.setattr(main_module, "verify_trace", fake_verify_trace)
    r = client.post("/infer", json=_payload(fixed_batch, request_id="req-integrity"))
    assert r.status_code == 500
    err = r.json()["error"]
    assert err["code"] == "REJECTED_CORE_INTEGRITY"
    assert err["verdict"] == "REJECTED"
    assert err["request_id"] == "req-integrity"
