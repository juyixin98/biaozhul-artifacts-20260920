"""Redaction and diagnostic identity tests."""

from __future__ import annotations

import numpy as np
import pytest

from service.logging_conf import redact_context

pytestmark = pytest.mark.unit


def test_redaction_keeps_scalars_and_shapes_but_hides_payloads() -> None:
    payload = np.arange(100_000, dtype=np.float64)
    safe = redact_context(
        {
            "request_id": "req-abc",
            "k_dim": 200_000,
            "observed_max": 3.2e9,
            "shape": (1, 2, 3),
            "weights": payload,
            "nested": {"secret": [1, 2, 3, 4]},
            "long_list": [1, 2, 3, 4, 5, 6, 7, 8, 9],
        }
    )
    assert safe["request_id"] == "req-abc"
    assert safe["k_dim"] == 200_000
    assert safe["shape"] == [1, 2, 3]
    assert safe["weights"].startswith("<redacted:ndarray")
    assert safe["nested"].startswith("<redacted:dict")
    # Lengthy integer lists are treated as potential payloads too.
    assert safe["long_list"].startswith("<redacted:list")


def test_redaction_handles_numpy_scalars() -> None:
    safe = redact_context({"v": np.int64(5), "f": np.float64(0.25)})
    assert safe == {"v": 5, "f": 0.25}
