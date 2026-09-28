"""Security/robustness regression tests found in the external review.

Covers: out-of-range integer and float64 overflow inputs, body-size cap,
request-id log-injection sanitization, parameter-vs-matrix error taxonomy,
and the rule that an indeterminate conditioning gate can never fake success.
"""

import numpy as np
import pytest

httpx = pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from sym_eig.api.app import create_app  # noqa: E402
from sym_eig.config import Settings  # noqa: E402
from sym_eig.errors import ErrorCategory  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app(Settings(max_n=64)))


def test_huge_python_integer_is_classified_not_500(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1, 10 ** 400], [0, 1]]},
    )
    assert resp.status_code == 422
    assert resp.json()["error_category"] == "VALUE_OUT_OF_RANGE"


@pytest.mark.parametrize("entry", [1e300, 1e308])
def test_finite_but_arithmetic_overflow_entries_rejected(client, entry):
    matrix = [[float(entry), 0.0, 0.0],
              [0.0, float(entry), 0.0],
              [0.0, 0.0, 1.0]]
    resp = client.post(
        "/api/v1/eigendecomposition", json={"matrix": matrix}
    )
    assert resp.status_code == 422
    assert resp.json()["error_category"] == "VALUE_OUT_OF_RANGE"


def test_large_but_safe_scale_still_succeeds(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1e150, 0.0], [0.0, 2e150]]},
    )
    assert resp.status_code == 200
    assert resp.json()["verdict"] == "SUCCESS"


def test_request_id_log_injection_is_sanitized(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1.0, 0.0], [0.0, 2.0]]},
        headers={"X-Request-ID": "abc\r\nINJECTED evil\u001b[31m"},
    )
    rid = resp.json()["request_id"]
    assert "\r" not in rid and "\n" not in rid and "\x1b" not in rid
    assert "evil" in rid  # kept, but control chars replaced


def test_bad_option_is_invalid_parameter_not_invalid_matrix(client):
    resp = client.post(
        "/api/v1/eigendecomposition",
        json={"matrix": [[1.0, 0.0], [0.0, 2.0]], "max_iters": "five"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_category"] == "INVALID_PARAMETER"


def test_bad_matrix_field_is_invalid_matrix(client):
    resp = client.post(
        "/api/v1/eigendecomposition", json={"matrix": "not-an-array"}
    )
    assert resp.status_code == 422
    assert resp.json()["error_category"] == "INVALID_MATRIX"


def test_oversized_body_rejected_before_parsing(client):
    settings = Settings(max_n=64)
    limit = 64 ** 2 * 64 + (1 << 16)
    # One long row declared larger than the byte cap.
    payload = '{"matrix": [[' + ",".join(["1"] * (limit // 2)) + "]]}"
    resp = client.post(
        "/api/v1/eigendecomposition",
        content=payload.encode(),
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 422
    assert resp.json()["error_category"] == "SIZE_LIMIT_EXCEEDED"


def test_indeterminate_conditioning_gate_never_passes():
    # ||A|| ~ 1e150 with an eigenvalue near zero: the Davis-Kahan eigenspace
    # bound far exceeds 0.5, so the independent eigenspace cross-check is
    # indeterminate and must be SKIPped (forcing UNCERTAIN), not passed.
    rng = np.random.default_rng(0)
    q = np.linalg.qr(rng.standard_normal((3, 3)))[0]
    matrix = (q * np.array([0.0, 1.0, 1e150])) @ q.T
    resp = TestClient(create_app(Settings(max_n=64))).post(
        "/api/v1/eigendecomposition", json={"matrix": matrix.tolist()}
    )
    body = resp.json()
    assert body["verdict"] == "UNCERTAIN"
    sub_gate = next(
        g for g in body["gates"] if g["name"] == "reference_subspace_match"
    )
    assert sub_gate["status"] == "SKIP"
    assert sub_gate["passed"] is False
    # The kernel answer itself remains backward stable.
    assert body["evidence"]["residual_relative_fro"] < 1e-9


def test_uncertain_category_maps_to_http_200_in_table():
    from sym_eig.errors import HTTP_STATUS
    assert HTTP_STATUS[ErrorCategory.UNCERTAIN_RESULT] == 200
    for category in (
        ErrorCategory.INVALID_MATRIX,
        ErrorCategory.INVALID_PARAMETER,
        ErrorCategory.NON_SYMMETRIC,
        ErrorCategory.SIZE_LIMIT_EXCEEDED,
        ErrorCategory.VALUE_OUT_OF_RANGE,
        ErrorCategory.NON_CONVERGENCE,
    ):
        assert HTTP_STATUS[category] == 422
