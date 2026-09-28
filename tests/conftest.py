"""Shared pytest fixtures and builders."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from twosls.contract import (  # noqa: E402
    EstimationOptions,
    EstimationRequest,
    InstrumentValidityClaim,
    ModelSpec,
)
from experiments.dgp import SyntheticSample  # noqa: E402


def sample_to_columns(sample: SyntheticSample, add_const: bool = True) -> dict[str, list[float]]:
    cols = {name: np.asarray(vec, dtype=float).tolist() for name, vec in sample.columns.items()}
    if add_const and "const" not in cols:
        n = len(next(iter(cols.values())))
        cols["const"] = [1.0] * n
    return cols


def make_spec(sample: SyntheticSample, *, instruments: int | None = None,
              exog_controls: bool = True) -> ModelSpec:
    L = instruments if instruments is not None else sample.config.n_instruments
    exog = ([f"w{j+1}" for j in range(sample.config.n_controls)] if exog_controls
            and sample.config.n_controls else [])
    exog = exog + ["const"]
    return ModelSpec(
        dependent="y",
        endogenous=["x_end"],
        included_exogenous=exog,
        excluded_instruments=[f"z{j+1}" for j in range(L)],
    )


def make_request(sample: SyntheticSample, *, request_id: str = "req-test",
                 covariance: str = "homoskedastic", strict: bool = False,
                 spec: ModelSpec | None = None, bootstrap_reps: int = 0,
                 assert_exclusion: bool = True) -> EstimationRequest:
    return EstimationRequest(
        request_id=request_id,
        columns=sample_to_columns(sample),
        spec=spec or make_spec(sample),
        options=EstimationOptions(
            covariance=covariance, strict=strict, bootstrap_reps=bootstrap_reps
        ),
        validity_claim=InstrumentValidityClaim(
            exclusion_restriction_asserted=assert_exclusion,
            rationale="synthetic DGP: Z generated independently of structural error e",
        ),
    )


@pytest.fixture
def strong_sample():
    from experiments.dgp import strong_iv_sample
    return strong_iv_sample()


@pytest.fixture
def weak_sample():
    from experiments.dgp import weak_iv_sample
    return weak_iv_sample()


@pytest.fixture
def collinear_sample():
    from experiments.dgp import collinear_iv_sample
    return collinear_iv_sample()


@pytest.fixture
def underidentified_sample_fixture():
    from experiments.dgp import underidentified_sample
    return underidentified_sample()


@pytest.fixture
def just_identified_sample_fixture():
    from experiments.dgp import just_identified_sample
    return just_identified_sample()
