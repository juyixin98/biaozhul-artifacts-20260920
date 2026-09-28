"""Pytest configuration: isolated local DB/log dirs and shared builders.

Env vars are set BEFORE any ``app`` module is imported, because
:mod:`app.config` resolves settings once at import time.  Every test therefore
runs against a throw-away SQLite file and a throw-away log directory - no
production data is ever touched.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TEST_ROOT = Path(tempfile.mkdtemp(prefix="spm-tests-"))
os.environ["SPM_DB_PATH"] = str(_TEST_ROOT / "test.db")
os.environ["SPM_LOG_DIR"] = str(_TEST_ROOT / "logs")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.corpus.spec import normalize_corpus  # noqa: E402
from app.models import CorpusCreateRequest, EventIn, SequenceIn  # noqa: E402


def make_events(symbols_with_ts):
    return [
        EventIn(symbol=s, timestamp=t) for s, t in symbols_with_ts
    ]


def make_request(sequences, name="test-corpus"):
    """``sequences``: dict seq_id -> list[(symbol, timestamp_or_None)]."""
    return CorpusCreateRequest(
        name=name,
        sequences=[
            SequenceIn(sequence_id=sid, events=make_events(events))
            for sid, events in sequences.items()
        ],
    )


def build_corpus(sequences, corpus_id="corpus-test"):
    return normalize_corpus(corpus_id, make_request(sequences))


@pytest.fixture(scope="session")
def test_root() -> Path:
    return _TEST_ROOT


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "isolated.db"
    app = create_app(db_path=str(db_path))
    # raise_server_exceptions=False so the global 500 handler's response is
    # observable and assertable instead of being re-raised into the test.
    with TestClient(app, raise_server_exceptions=False) as c:
        c.db_path = str(db_path)  # exposed for assertions / inspection
        yield c


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "repo.db")


# ---------------------------------------------------------- kernel-level helper

def run_kernel(sequences, *, min_support=1, max_gap_position=None,
               max_gap_time=None, max_pattern_length=None,
               include_embeddings=True, corpus_id="corpus-kernel"):
    """Invoke the mining kernel directly (no HTTP, no SQLite)."""
    from app.miner.constraints import validate_query
    from app.miner.facade import run_mining
    from app.models import MineRequest
    from app.observability import get_run_logger

    corpus = build_corpus(sequences, corpus_id=corpus_id)
    request = MineRequest(
        corpus_id=corpus_id,
        min_support=min_support,
        max_gap_position=max_gap_position,
        max_gap_time=max_gap_time,
        max_pattern_length=max_pattern_length,
        include_embeddings=include_embeddings,
    )
    constraints = validate_query(corpus, request)
    logger = get_run_logger(prefix="mine", file_logging=False)
    response = run_mining(
        corpus, constraints, logger, run_id=logger.run_id,
        include_embeddings=include_embeddings,
    )
    logger.close()
    return response


def index_by_pattern(response):
    return {pr.pattern: pr for pr in response.patterns}
