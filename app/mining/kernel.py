"""Prefix-projection growth miner.

The projected "database" for a prefix is, per sequence, the SET OF ALL
embeddings of that prefix (not just the first/shortest one). Extending from
every retained embedding is what keeps alternative embeddings alive: an
embedding that ends early may not be extendable within the gap window while
a later one is, and pruning it would wrongly drop the pattern's support.

Support is counted per sequence identity: a sequence contributes 1 to the
support of a pattern no matter how many embeddings it contains.
"""

from __future__ import annotations

import logging

from app.mining.constraints import gap_satisfies, validate_constraints
from app.models.domain import (
    EmbeddingEvidence,
    GapConstraints,
    PatternResult,
    Sequence,
)

# seq_id -> set of embeddings (position tuples) of the current prefix
Projection = dict[str, set[tuple[int, ...]]]


class PrefixGrowthMiner:
    def __init__(
        self,
        sequences: list[Sequence],
        constraints: GapConstraints,
        min_support: int,
        max_pattern_len: int = 8,
        max_embeddings_per_sequence: int = 64,
        logger: logging.Logger | None = None,
        run_id: str | None = None,
    ):
        if min_support < 1:
            raise ValueError("min_support must be >= 1")
        validate_constraints(constraints, sequences)
        self._sequences = sequences
        self._by_id = {s.sequence_id: s for s in sequences}
        self._constraints = constraints
        self._min_support = min_support
        self._max_pattern_len = max_pattern_len
        self._max_embeddings = max_embeddings_per_sequence
        self._log = logger or logging.getLogger("fspm.mining")
        self._run_id = run_id

    def mine(self) -> list[PatternResult]:
        self._log.info(
            "mine_start",
            extra={
                "run_id": self._run_id,
                "step": "mine_start",
                "detail": {
                    "sequence_count": len(self._sequences),
                    "min_support": self._min_support,
                    "max_pos_gap": self._constraints.max_pos_gap,
                    "max_time_gap": self._constraints.max_time_gap,
                    "max_pattern_len": self._max_pattern_len,
                },
            },
        )
        results: list[PatternResult] = []
        initial: Projection = {s.sequence_id: {()} for s in self._sequences}
        self._grow((), initial, results)
        results.sort(key=lambda r: (len(r.pattern), r.pattern))
        self._log.info(
            "mine_done",
            extra={
                "run_id": self._run_id,
                "step": "mine_done",
                "detail": {"frequent_pattern_count": len(results)},
            },
        )
        return results

    def _successor_positions(self, seq: Sequence, emb: tuple[int, ...]):
        start = emb[-1] + 1 if emb else 0
        for j in range(start, len(seq.events)):
            if emb and not gap_satisfies(seq.events, emb[-1], j, self._constraints):
                break  # gaps widen monotonically in j
            yield j

    def _grow(
        self,
        prefix: tuple[str, ...],
        projection: Projection,
        results: list[PatternResult],
    ) -> None:
        if len(prefix) >= self._max_pattern_len:
            self._log.info(
                "max_pattern_len reached, stopping recursion",
                extra={
                    "run_id": self._run_id,
                    "step": "depth_limit",
                    "pattern": list(prefix),
                },
            )
            return
        # candidate symbol -> projected embeddings per sequence
        candidates: dict[str, dict[str, set[tuple[int, ...]]]] = {}
        for seq_id, embeddings in projection.items():
            seq = self._by_id[seq_id]
            for emb in embeddings:
                for j in self._successor_positions(seq, emb):
                    symbol = seq.events[j].symbol
                    candidates.setdefault(symbol, {}).setdefault(seq_id, set()).add(
                        emb + (j,)
                    )
        for symbol in sorted(candidates):
            proj = candidates[symbol]
            support = len(proj)  # sequence identity, not occurrence count
            pattern = prefix + (symbol,)
            decision = "keep" if support >= self._min_support else "prune"
            self._log.info(
                "pattern_eval",
                extra={
                    "run_id": self._run_id,
                    "step": "pattern_eval",
                    "pattern": list(pattern),
                    "support": support,
                    "min_support": self._min_support,
                    "decision": decision,
                },
            )
            if decision == "keep":
                results.append(self._build_result(pattern, proj))
                self._grow(pattern, proj, results)

    def _build_result(self, pattern: tuple[str, ...], proj: Projection) -> PatternResult:
        evidence: list[EmbeddingEvidence] = []
        complete = True
        for seq_id in sorted(proj):
            embeddings = sorted(proj[seq_id])
            if len(embeddings) > self._max_embeddings:
                embeddings = embeddings[: self._max_embeddings]
                complete = False
            evidence.extend(
                EmbeddingEvidence(sequence_id=seq_id, positions=emb)
                for emb in embeddings
            )
        return PatternResult(
            pattern=pattern,
            support=len(proj),
            embeddings=evidence,
            evidence_complete=complete,
        )
