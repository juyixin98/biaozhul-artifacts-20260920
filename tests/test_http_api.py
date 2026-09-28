"""End-to-end HTTP tests against the FastAPI application.

These cover the transport contract: status codes, request-id propagation,
body-size rejection, schema validation (without echoing coefficients), and
the full isolation response over real HTTP semantics via an in-process ASGI
client.
"""
from __future__ import annotations

import pytest

@pytest.mark.anyio
async def test_health_ok(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "up"


@pytest.mark.anyio
async def test_isolate_end_to_end(client):
    payload = {"coefficients": [-6, 11, -6, 1], "target_width": "1/100000"}
    response = await client.post("/api/v1/isolate-real-roots", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["degree"] == 3
    assert len(data["roots"]) == 3
    assert data["evidence"]["accepted"] is True
    # Request id is generated and echoed via header and body.
    assert data["request_id"]
    assert response.headers["x-request-id"] == data["request_id"]


@pytest.mark.anyio
async def test_zero_polynomial_special_case_over_http(client):
    response = await client.post(
        "/api/v1/isolate-real-roots", json={"coefficients": [0, 0, 0]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "zero_polynomial"
    assert data["is_zero_polynomial"] is True
    assert data["roots"] == []


@pytest.mark.anyio
async def test_client_request_id_is_honored(client):
    response = await client.post(
        "/api/v1/isolate-real-roots",
        json={"coefficients": [1, 0, -1]},
        headers={"X-Request-ID": "trace-xyz"},
    )
    data = response.json()
    assert data["request_id"] == "trace-xyz"
    assert response.headers["x-request-id"] == "trace-xyz"


@pytest.mark.anyio
async def test_wrong_structural_type_returns_422_without_input_echo(client):
    # A string where the array belongs is a schema (structural) failure.
    response = await client.post(
        "/api/v1/isolate-real-roots",
        json={"coefficients": "not-an-array"},
    )
    assert response.status_code == 422
    data = response.json()
    assert data["status"] == "rejected"
    assert data["failure"]["code"] == "INVALID_COEFFICIENTS"
    # The custom handler strips 'input', so the bad value is never echoed.
    assert "not-an-array" not in response.text


@pytest.mark.anyio
async def test_empty_array_422_at_schema(client):
    response = await client.post(
        "/api/v1/isolate-real-roots", json={"coefficients": []},
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_extra_field_rejected_422(client):
    response = await client.post(
        "/api/v1/isolate-real-roots",
        json={"coefficients": [1, 0, -1], "unexpected": 7},
    )
    assert response.status_code == 422
    assert response.json()["failure"]["code"] == "INVALID_COEFFICIENTS"


@pytest.mark.anyio
async def test_missing_coefficients_422(client):
    response = await client.post(
        "/api/v1/isolate-real-roots", json={"target_width": "1/10"},
    )
    assert response.status_code == 422


@pytest.mark.anyio
async def test_rejected_status_for_bad_coefficients_stays_200_with_status(client):
    # Syntactically valid JSON array, semantically bad token: the service
    # classifies it as rejected (transport 200, body status rejected).
    response = await client.post(
        "/api/v1/isolate-real-roots",
        json={"coefficients": ["@@@"]},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "rejected"
    assert data["failure"]["code"] == "INVALID_COEFFICIENTS"


@pytest.mark.anyio
async def test_endpoint_roots_case_over_http(client):
    payload = {
        "coefficients": [54, -189, 261, -182, 68, -13, 1],
        "target_width": "1/1000000",
        "interval_lo": "1",
        "interval_hi": "3",
    }
    response = await client.post("/api/v1/isolate-real-roots", json=payload)
    data = response.json()
    assert data["status"] == "ok"
    exact = {r["lo"] for r in data["roots"] if r["exact"]}
    assert exact == {"1", "2", "3"}


@pytest.mark.anyio
async def test_openapi_documents_endpoint(client):
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/isolate-real-roots" in paths


@pytest.mark.anyio
async def test_oversized_body_rejected_413(settings):
    import dataclasses
    tiny_http = dataclasses.replace(settings.http, max_body_bytes=128)
    limited = dataclasses.replace(settings, http=tiny_http)
    from app.main import create_app as make_app
    from httpx import ASGITransport, AsyncClient
    app = make_app(limited)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        big = {"coefficients": ["1/3"] * 10000}
        response = await ac.post("/api/v1/isolate-real-roots", json=big)
    assert response.status_code == 413
    data = response.json()
    assert data["failure"]["code"] == "PAYLOAD_TOO_LARGE"
    assert data["failure"]["state"]["budget_bytes"] == 128


@pytest.mark.anyio
async def test_failure_classification_body_shape(client):
    # Invalid polynomial token: rejected classification with state present.
    response = await client.post(
        "/api/v1/isolate-real-roots", json={"coefficients": ["@@@"]},
    )
    data = response.json()
    assert data["status"] == "rejected"
    assert data["diagnostics"]["decision"] == "rejected"
    assert data["failure"]["code"] == "INVALID_COEFFICIENTS"
    assert "power" in data["failure"]["state"]
