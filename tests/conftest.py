"""Shared pytest fixtures and builders."""

from __future__ import annotations

import numpy as np
import pytest

from app.config import ServiceConfig
from app.contracts import EstimateRequest
from app.dgp import DGPSpec, generate, standard_names


@pytest.fixture
def config(tmp_path) -> ServiceConfig:
    return ServiceConfig(
        max_obs=200_000,
        db_path=str(tmp_path / "test.db"),
        log_level="WARNING",
        log_raw_data=False,
        data_hash_prefix_len=12,
        default_cov_type="conventional",
        significance=0.05,
        weak_rule="stock_yogo_10",
        rank_rcond=1e-9,
        vif_warn=30.0,
        hc_small_sample=True,
        overid_significance=0.05,
        endogeneity_significance=0.05,
    )


@pytest.fixture
def build_request():
    def _build(spec: DGPSpec, *, cov_type: str = "conventional",
               declared: bool = True, columns=None, **overrides) -> EstimateRequest:
        ds = generate(spec)
        nm = standard_names(spec)
        payload = dict(
            dependent="y",
            endogenous=nm["endogenous"],
            exogenous=nm["exogenous"],
            instruments=nm["instruments"],
            columns=columns if columns is not None else ds.columns,
            cov_type=cov_type,
            assume_exclusion_restriction=declared,
            exclusion_rationale="synthetic experiment" if declared else None,
        )
        payload.update(overrides)
        return EstimateRequest(**payload)

    return _build


@pytest.fixture
def raw_arrays():
    """Generate raw numpy arrays directly (tests build matrices themselves)."""
    def _generate(spec: DGPSpec) -> dict[str, np.ndarray | tuple]:
        ds = generate(spec)
        nm = standard_names(spec)
        out = {
            "y": np.asarray(ds.columns["y"]),
            "X": np.column_stack([ds.columns[n] for n in nm["endogenous"]]),
            "W": np.column_stack([ds.columns[n] for n in nm["exogenous"]]),
            "Z": np.column_stack([ds.columns[n] for n in nm["instruments"]]),
            "true_beta": np.asarray(ds.true_beta),
            "true_gamma": np.asarray(ds.true_gamma),
        }
        return out

    return _generate
