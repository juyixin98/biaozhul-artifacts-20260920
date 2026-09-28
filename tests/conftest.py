"""Shared pytest fixtures and small factories."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from sparse_embedding.config import (  # noqa: E402
    ClipConfig,
    ClipMode,
    OptimizerConfig,
    OptimizerName,
    ServiceConfig,
    TableSpec,
)
from sparse_embedding.journal import RunJournal  # noqa: E402
from sparse_embedding.service import SparseEmbeddingService  # noqa: E402
from sparse_embedding.state import TrainingState  # noqa: E402


@pytest.fixture
def results_dir(tmp_path: Path) -> Path:
    d = tmp_path / "results"
    d.mkdir()
    return d


@pytest.fixture
def journal(results_dir: Path) -> RunJournal:
    return RunJournal(str(results_dir / "journal.jsonl"))


def make_config(
    *,
    num_rows: int = 8,
    dim: int = 3,
    lr: float = 0.1,
    momentum: float = 0.9,
    name: OptimizerName = OptimizerName.SGD_MOMENTUM,
    weight_decay: float = 0.0,
    clip_mode: ClipMode = ClipMode.NONE,
    max_norm: float | None = None,
    state_dir: str,
    seed: int = 20260928,
) -> ServiceConfig:
    return ServiceConfig(
        table=TableSpec(num_rows=num_rows, dim=dim),
        optimizer=OptimizerConfig(
            name=name, lr=lr, momentum=momentum, weight_decay=weight_decay
        ),
        clip=ClipConfig(mode=clip_mode, max_norm=max_norm),
        state_dir=state_dir,
        seed=seed,
    )


@pytest.fixture
def make_service(journal, tmp_path):
    def _factory(**overrides):
        cfg = make_config(state_dir=str(tmp_path / "state"), **overrides)
        return SparseEmbeddingService(cfg, journal)

    return _factory


@pytest.fixture
def make_state():
    def _factory(*, num_rows=8, dim=3, seed=20260928):
        return TrainingState(TableSpec(num_rows=num_rows, dim=dim), seed=seed)

    return _factory


@pytest.fixture
def initial_table(make_state):
    """A fresh small dense table shared as the starting point for oracle
    cross-checks (values captured, not regenerated)."""

    state = make_state(num_rows=8, dim=3)
    return state.weights.copy()
