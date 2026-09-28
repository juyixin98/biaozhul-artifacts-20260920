"""HTTP service tests via Starlette's in-process TestClient (no network)."""

from __future__ import annotations

import numpy as np
from fastapi.testclient import TestClient

from toeplitz_fft.fixtures import (
    fixture_to_payload,
    make_asymmetric_real,
    make_complex_non_hermitian,
    make_tiny_case,
)
from toeplitz_fft.service import create_app


def _client() -> TestClient:
    return TestClient(create_app())


def test_health_reports_ok_and_run_identity() -> None:
    with _client() as client:
        resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["run_id"]


def test_metadata_exposes_versions_and_kernel_digest() -> None:
    with _client() as client:
        body = client.get("/metadata").json()
    assert body["versions"]["numpy"] == np.__version__
    assert len(body["kernel"]["kernel_digest"]) == 16


def test_compute_real_asymmetric_non_power_of_two() -> None:
    fx = make_asymmetric_real(13, batch=2)
    with _client() as client:
        resp = client.post("/compute", json=fixture_to_payload(fx))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["plan"]["embedding_m"] >= 2 * 13 - 1
    assert body["plan"]["path"] == "embedding_fft"
    result = np.asarray(body["result"])
    assert result.shape == (2, 13)
    # independent dense check inside the test itself
    from scipy.linalg import toeplitz
    expected = fx.vectors @ toeplitz(fx.first_column, fx.first_row).T
    np.testing.assert_allclose(result, expected, rtol=1e-10, atol=1e-10)


def test_second_compute_call_is_cache_hit() -> None:
    payload = fixture_to_payload(make_asymmetric_real(13))
    with _client() as client:
        first = client.post("/compute", json=payload).json()
        second = client.post("/compute", json=payload).json()
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True


def test_compute_complex_mode_returns_pairs() -> None:
    fx = make_complex_non_hermitian(17)
    with _client() as client:
        resp = client.post("/compute", json=fixture_to_payload(fx))
    assert resp.status_code == 200
    # result shape is (batch, n, 2) where each element is a [real, imag] pair
    result = np.array([[complex(*pair) for pair in row]
                       for row in resp.json()["result"]])
    from scipy.linalg import toeplitz
    expected = fx.vectors @ toeplitz(fx.first_column, fx.first_row).T
    np.testing.assert_allclose(result, expected, rtol=1e-10, atol=1e-10)


def test_verify_endpoint_passes_and_repeats_run_id() -> None:
    fx = make_asymmetric_real(13)
    with _client() as client:
        resp = client.post("/verify", json=fixture_to_payload(fx))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["evidence"]["passed"] is True
    assert body["evidence"]["case_id"] == body["run_id"]
    assert body["evidence"]["against_mpmath"]["max_abs_error"] < 1e-10


def test_shared_element_mismatch_returns_categorized_422() -> None:
    payload = fixture_to_payload(make_tiny_case(1))
    payload["first_column"][0] = 99.0  # break c[0] == r[0]
    with _client() as client:
        resp = client.post("/compute", json=payload)
    assert resp.status_code == 422
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "first_element_mismatch"
    assert body["run_id"]


def test_malformed_json_is_bad_request_not_success() -> None:
    with _client() as client:
        resp = client.post("/compute", content="{not json",
                           headers={"content-type": "application/json"})
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "bad_request"


def test_tiny_problem_takes_explicit_path() -> None:
    with _client() as client:
        resp = client.post("/compute", json=fixture_to_payload(make_tiny_case(1)))
    body = resp.json()
    assert body["plan"]["path"] == "tiny_explicit"
    np.testing.assert_allclose(body["result"], [[7.0], [-14.0]])
