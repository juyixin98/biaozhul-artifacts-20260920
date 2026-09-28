"""Service tests: HTTP contract, concrete values, typed error responses."""

import numpy as np
import pytest
from fastapi.testclient import TestClient

from toeplitz_fft.service import create_app


@pytest.fixture()
def client():
    return TestClient(create_app())


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_meta_reports_versions_and_cache(client):
    resp = client.get("/v1/meta")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["versions"]) >= {"python", "numpy", "scipy", "mpmath"}
    assert body["config"]["real_dtype"] == "float64"


def test_matvec_hand_case_over_http(client):
    resp = client.post("/v1/toeplitz/matvec", json={
        "c": [1.0, 2.0, 3.0], "r": [1.0, 4.0], "x": [5.0, 6.0],
    })
    assert resp.status_code == 200
    body = resp.json()
    np.testing.assert_allclose(body["y"], [29.0, 16.0, 27.0], rtol=1e-12)
    meta = body["meta"]
    assert meta["mode"] == "real"
    assert meta["L"] == 4
    assert meta["cache_hit"] is False
    assert len(meta["kernel_digest"]) == 64
    assert meta["request_id"]


def test_matvec_second_identical_call_reports_cache_hit(client):
    payload = {"c": [1.0, 2.0, 3.0], "r": [1.0, 4.0], "x": [5.0, 6.0]}
    client.post("/v1/toeplitz/matvec", json=payload)
    body = client.post("/v1/toeplitz/matvec", json=payload).json()
    assert body["meta"]["cache_hit"] is True


def test_matvec_complex_over_http(client):
    resp = client.post("/v1/toeplitz/matvec", json={
        "c": [[1.0, 1.0], [2.0, 0.0]],
        "r": [[1.0, 1.0], [0.0, 3.0]],
        "x": [[1.0, 0.0], [0.0, 1.0]],
    })
    assert resp.status_code == 200
    body = resp.json()
    # T = [[1+i, 3i], [2, 1+i]]; x = [1, i]
    # y0 = (1+i)*1 + 3i*i = 1+i-3 = -2+i ; y1 = 2*1 + (1+i)*i = 2+i-1 = 1+i
    np.testing.assert_allclose(body["y"], [[-2.0, 1.0], [1.0, 1.0]], rtol=1e-12)
    assert body["meta"]["mode"] == "complex"
    assert body["meta"]["dtype"] == "complex128"


def test_matmat_batch_over_http(client):
    resp = client.post("/v1/toeplitz/matmat", json={
        "c": [1.0, 2.0, 3.0], "r": [1.0, 4.0],
        "X": [[5.0, 6.0], [1.0, 0.0]],  # two column vectors
    })
    assert resp.status_code == 200
    body = resp.json()
    np.testing.assert_allclose(body["Y"][0], [29.0, 16.0, 27.0], rtol=1e-12)
    np.testing.assert_allclose(body["Y"][1], [1.0, 2.0, 3.0], rtol=1e-12)
    assert body["meta"]["k"] == 2


def test_inconsistent_shared_element_returns_400_with_category(client):
    resp = client.post("/v1/toeplitz/matvec", json={
        "c": [1.0, 2.0], "r": [9.0, 4.0], "x": [1.0, 1.0],
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "inconsistent_shared_element"


def test_wrong_vector_length_returns_400_shape_mismatch(client):
    resp = client.post("/v1/toeplitz/matvec", json={
        "c": [1.0, 2.0], "r": [1.0, 4.0], "x": [1.0, 1.0, 1.0],
    })
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "shape_mismatch"


def test_empty_first_column_rejected_by_schema(client):
    resp = client.post("/v1/toeplitz/matvec", json={
        "c": [], "r": [1.0], "x": [1.0],
    })
    assert resp.status_code == 422


def test_unknown_mode_rejected_by_schema(client):
    resp = client.post("/v1/toeplitz/matvec", json={
        "c": [1.0], "r": [1.0], "x": [1.0], "mode": "banana",
    })
    assert resp.status_code == 422
