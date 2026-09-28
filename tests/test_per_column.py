"""Per-column independent reporting for batched right-hand sides."""

from __future__ import annotations

import pytest

from app.config import Config
from app.numerical import mpf
from app.numerical.engine import solve_system
from tests.fixtures import directional_rhs_system

pytestmark = pytest.mark.unit


@pytest.fixture(scope="module")
def config() -> Config:
    return Config.load()


def test_batch_reports_each_column_independently(config):
    # cond ~ 1e8, two RHS aligned to the largest and smallest singular vectors.
    # With a one-iteration fp32 budget and tolerance between their first-iterate
    # backward errors, exactly one column passes. Their eta differ ~14x, so the
    # threshold choice has margin against roundoff drift across BLAS versions.
    a, b, _ = directional_rhs_system(10, 8, seed=42)
    cfg = config.with_overrides(
        {
            "use_fp32_first": True,
            "use_fp64": False,
            "mp_dps_ladder": [],
            "backward_tol": "5e-9",
            "max_iterations_per_stage": 1,
        }
    )
    result = solve_system(a, b, cfg, "percol-batch")

    assert result.status == "partially_accepted"
    assert len(result.columns) == 2
    statuses = {c.column: c.status for c in result.columns}
    assert statuses[0] == "not_met"
    assert statuses[1] == "accepted"

    eta0 = mpf(result.columns[0].best_eta)
    eta1 = mpf(result.columns[1].best_eta)
    assert eta1 < mpf("5e-9")
    assert eta0 > mpf("5e-9")
    assert eta0 / eta1 > mpf(5)

    # The accepted column carries stage/iteration; the rejected one neither.
    assert result.columns[1].accepted_at_stage == "float32"
    assert result.columns[1].accepted_at_iteration == 1
    assert result.columns[0].accepted_at_stage is None
    assert result.columns[0].accepted_at_iteration is None


def test_batch_event_log_has_one_eta_per_column(config):
    a, b, _ = directional_rhs_system(8, 6, seed=7)
    result = solve_system(a, b, config, "percol-events")
    for event in result.events:
        assert len(event.per_column_eta) == 2
