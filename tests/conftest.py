"""Shared pytest fixtures: load synthetic problems from fixtures/matrices/."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest
import scipy.sparse as sp

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "matrices"


def load_fixture(name: str):
    payload = json.loads((FIXTURE_DIR / f"{name}.json").read_text())
    A = sp.coo_matrix(
        (payload["data"], (payload["row"], payload["col"])),
        shape=(payload["n"], payload["n"]),
    ).tocsr()
    v = np.asarray(payload["vector"], dtype=np.float64)
    return payload, A, v


@pytest.fixture(scope="session")
def fixture_names() -> list[str]:
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))
