"""Mining facade: orchestrates projection engine + evidence enumeration.

The engine decides *which* patterns are frequent and *which sequence
identities* support them; the evidence enumerator independently produces the
concrete embeddings.  A defensive invariant check verifies that the two agree
on the supporting-sequence set — disagreement raises an internal error rather
than returning a misleading success.
"""
from __future__ import annotations

import time
from typing import Optional

from app.errors import MiningInternalError
from app.miner.constraints import ValidatedConstraints
from app.miner.engine import ProjectionMiner, SeqData, _index_sequence
from app.miner.evidence import build_embedding, enumerate_embeddings
from app.models import Corpus, MineResponse, PatternResult, SequenceSupport
from app.observability import RunLogger


def _sort_key(pattern: tuple[str, ...]):
    return (-len(pattern), pattern)


def run_mining(
    corpus: Corpus,
    constraints: ValidatedConstraints,
    logger: RunLogger,
    *,
    run_id: str,
    include_embeddings: bool = True,
) -> MineResponse:
    started = time.perf_counter()

    engine = ProjectionMiner(corpus, constraints, logger)
    raw_patterns = engine.mine()

    indexed: dict[str, SeqData] = {
        seq.sequence_id: _index_sequence(seq) for seq in corpus.sequences
    }
    self_indexed = list(indexed.values())

    results: list[PatternResult] = []
    for raw in raw_patterns:
        pattern = raw.pattern
        evidence_per_seq: list[SequenceSupport] = []
        all_occurrences = 0
        engine_support = set(raw.supporting)
        enumerator_support: set[str] = set()
        # Run the independent enumerator over EVERY sequence (not just the
        # engine's supporting set) so support sets can be cross-checked in
        # both directions.
        for data in self_indexed:
            embeddings_raw = enumerate_embeddings(data, pattern, constraints)
            if embeddings_raw:
                enumerator_support.add(data.sequence_id)
            if data.sequence_id in engine_support:
                if not embeddings_raw:
                    raise MiningInternalError(
                        "engine/evidence disagreement: projection claimed support "
                        "but enumerator found no embedding",
                        details={"run_id": run_id, "pattern": list(pattern),
                                 "sequence_id": data.sequence_id},
                    )
                all_occurrences += len(embeddings_raw)
                if include_embeddings:
                    evidence_per_seq.append(
                        SequenceSupport(
                            sequence_id=data.sequence_id,
                            embeddings=tuple(
                                build_embedding(data, pos) for pos in embeddings_raw
                            ),
                        )
                    )
                logger.decision(
                    "evidence-collected",
                    pattern=list(pattern),
                    sequence_id=data.sequence_id,
                    embedding_count=len(embeddings_raw),
                    positions=[list(pos) for pos in embeddings_raw],
                )

        if enumerator_support != engine_support:
            raise MiningInternalError(
                "engine/evidence disagreement on supporting-sequence set",
                details={
                    "run_id": run_id,
                    "pattern": list(pattern),
                    "engine_only": sorted(engine_support - enumerator_support),
                    "enumerator_only": sorted(enumerator_support - engine_support),
                },
            )

        supporting_ids = tuple(sorted(engine_support))
        results.append(
            PatternResult(
                pattern=pattern,
                length=len(pattern),
                support=len(supporting_ids),
                supporting_sequence_ids=supporting_ids,
                occurrences=all_occurrences,
                evidence=tuple(evidence_per_seq),
            )
        )

    results.sort(key=lambda pr: _sort_key(pr.pattern))
    duration_ms = (time.perf_counter() - started) * 1000
    logger.step("response-assembled", pattern_count=len(results),
                duration_ms=round(duration_ms, 3))
    return MineResponse(
        run_id=logger.run_id,
        corpus_id=corpus.corpus_id,
        corpus_size=corpus.size,
        constraints={
            "min_support": constraints.min_support,
            "max_gap_position": constraints.max_gap_position,
            "max_gap_time": constraints.max_gap_time,
            "max_pattern_length": constraints.max_pattern_length,
        },
        duration_ms=round(duration_ms, 3),
        pattern_count=len(results),
        patterns=results,
    )
