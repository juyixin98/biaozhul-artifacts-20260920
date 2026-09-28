"""Application service layer: orchestrates storage, spec validation and mining."""
from __future__ import annotations

import uuid

from app.corpus.fixtures import all_fixture_requests
from app.corpus.spec import corpus_has_timestamps, normalize_corpus
from app.errors import NotFoundError
from app.miner.constraints import validate_query
from app.miner.facade import run_mining
from app.models import Corpus, MineRequest, MineResponse
from app.observability import RunLogger
from app.storage.repository import CorpusRepository


def _new_corpus_id() -> str:
    return f"corpus-{uuid.uuid4().hex[:12]}"


class MiningService:
    """Single facade used by the HTTP layer; no HTTP types leak in here."""

    def __init__(self, repository: CorpusRepository | None = None) -> None:
        self.repo = repository or CorpusRepository()

    # ------------------------------------------------------------------ corpus

    def create_corpus(self, request) -> Corpus:
        corpus_id = _new_corpus_id()
        corpus = normalize_corpus(corpus_id, request)
        self.repo.save_corpus(corpus, has_timestamps=corpus_has_timestamps(corpus))
        return corpus

    def load_fixture(self, fixture_name: str) -> Corpus:
        fixtures = all_fixture_requests()
        if fixture_name not in fixtures:
            raise NotFoundError(
                "unknown synthetic fixture",
                details={"fixture": fixture_name, "available": sorted(fixtures)},
            )
        corpus_id = f"fixture-{fixture_name}-{uuid.uuid4().hex[:8]}"
        corpus = normalize_corpus(corpus_id, fixtures[fixture_name])
        self.repo.save_corpus(corpus, has_timestamps=corpus_has_timestamps(corpus))
        return corpus

    def list_corpora(self) -> list[dict]:
        return self.repo.list_corpora()

    def get_corpus(self, corpus_id: str) -> Corpus:
        return self.repo.get_corpus(corpus_id)

    def delete_corpus(self, corpus_id: str) -> None:
        self.repo.delete_corpus(corpus_id)

    # ------------------------------------------------------------------ mining

    def mine(self, request: MineRequest, logger: RunLogger) -> MineResponse:
        corpus = self.repo.get_corpus(request.corpus_id)
        constraints = validate_query(corpus, request)
        logger.info(
            "query-validated",
            corpus_id=corpus.corpus_id,
            corpus_size=corpus.size,
            min_support=constraints.min_support,
            max_gap_position=constraints.max_gap_position,
            max_gap_time=constraints.max_gap_time,
            max_pattern_length=constraints.max_pattern_length,
        )
        return run_mining(
            corpus,
            constraints,
            logger,
            run_id=logger.run_id,
            include_embeddings=request.include_embeddings,
        )


def build_service(*, db_path: str | None = None) -> MiningService:
    return MiningService(CorpusRepository(db_path=db_path))
