"""PrefixSpan-style projection database engine.

Algorithm
=========

A pattern is grown one symbol at a time by *projecting* the database onto a
prefix.  The crucial correctness device is what a projection stores:

* a **seed projection** for a symbol ``x`` maps each sequence containing ``x``
  to the set of suffix start indices ``{j | seq[j].symbol == x}`` — one entry
  per occurrence, never just the first;
* extending pattern prefix ``P`` by symbol ``y`` maps a sequence's suffix set
  ``S`` to ``{j | seq[j].symbol == y and exists q in S with q < j and
  pair_ok(q, j)}``.

Because every suffix start that can follow *some* embedding of the prefix is
retained, alternative embeddings can never be pruned: a start ``j`` is kept
even if another suffix start dominates it for the current pattern, because it
may be the only predecessor a later symbol can reach under the gap limits.
Support of a pattern is the number of **distinct sequence ids** with a
non-empty projection — repeated symbols and multiple embeddings never inflate
it.

Evidence (all embeddings) is reconstructed separately by enumerating all
constraint-satisfying paths through the same transition relation, so the
support bookkeeping and the proof objects share semantics but not code.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import Optional

from app import __version__
from app.miner.constraints import ValidatedConstraints
from app.models import Corpus, Sequence
from app.observability import RunLogger


@dataclass(frozen=True)
class SeqData:
    """Indexed view of one sequence for fast matching."""

    sequence_id: str
    symbols: tuple[str, ...]
    positions: tuple[int, ...]
    timestamps: tuple[Optional[float], ...]


def _index_sequence(seq: Sequence) -> SeqData:
    return SeqData(
        sequence_id=seq.sequence_id,
        symbols=tuple(e.symbol for e in seq.events),
        positions=tuple(e.position for e in seq.events),
        timestamps=tuple(e.timestamp for e in seq.events),
    )


def _legal_successors(
    data: SeqData, q: int, symbol: str, c: ValidatedConstraints
) -> frozenset[int]:
    """Positions of ``symbol`` reachable after projector position ``q``.

    Scans strictly forward (``p > q``).  With a position-gap limit the scan is
    bounded to ``q + max_gap_position``; with only a time limit (or none) it
    can stop at the first event whose timestamp exceeds the limit, because
    timestamps are non-decreasing.
    """
    limit_p = c.max_gap_position
    hard_stop = min(len(data.symbols), q + limit_p + 1) if limit_p is not None else len(data.symbols)
    tq = data.timestamps[q]
    out: set[int] = set()
    for p in range(q + 1, hard_stop):
        if c.max_gap_time is not None:
            tp = data.timestamps[p]
            if tp is None or (tp - tq) > c.max_gap_time:
                break  # non-decreasing timestamps: nothing later can satisfy it
        if data.symbols[p] == symbol:
            out.add(p)
    return frozenset(out)


def _project(
    data: SeqData, suffix_starts: frozenset[int], symbol: str,
    c: ValidatedConstraints,
) -> frozenset[int]:
    """All positions of ``symbol`` reachable after *some* prefix embedding."""
    union: set[int] = set()
    for q in suffix_starts:
        union.update(_legal_successors(data, q, symbol, c))
    return frozenset(union)


def _seed_projection(data: SeqData, symbol: str) -> frozenset[int]:
    return frozenset(p for p, s in enumerate(data.symbols) if s == symbol)


@dataclass(frozen=True)
class RawPattern:
    pattern: tuple[str, ...]
    supporting: dict[str, frozenset[int]]  # sequence id -> suffix-start set


class ProjectionMiner:
    """Runs the depth-first projection growth over one corpus."""

    def __init__(self, corpus: Corpus, constraints: ValidatedConstraints,
                 logger: RunLogger) -> None:
        self._corpus = corpus
        self._c = constraints
        self._logger = logger
        self._sequences = [_index_sequence(s) for s in corpus.sequences]
        self._results: list[RawPattern] = []

    def mine(self) -> list[RawPattern]:
        c = self._c
        vocabulary = sorted({s for data in self._sequences for s in data.symbols})
        self._logger.step(
            "mining-start",
            service_version=__version__,
            python_version=sys.version.split()[0],
            corpus_id=self._corpus.corpus_id,
            sequences=len(self._sequences),
            vocabulary_size=len(vocabulary),
            min_support=c.min_support,
            max_gap_position=c.max_gap_position,
            max_gap_time=c.max_gap_time,
            max_pattern_length=c.max_pattern_length,
        )

        depth = 0
        frontier: list[RawPattern] = []
        for symbol in vocabulary:
            supporting: dict[str, frozenset[int]] = {}
            for data in self._sequences:
                proj = _seed_projection(data, symbol)
                if proj:
                    supporting[data.sequence_id] = proj
            support = len(supporting)
            support = len(supporting)
            if support >= c.min_support:
                self._logger.decision(
                    "seed-support",
                    symbol=symbol,
                    support=support,
                    min_support=c.min_support,
                    qualifies=True,
                    supporting_sequences=sorted(supporting),
                )
                node = RawPattern(pattern=(symbol,), supporting=supporting)
                frontier.append(node)
            else:
                self._logger.decision(
                    "pattern-pruned",
                    pattern=[symbol],
                    support=support,
                    min_support=c.min_support,
                    qualifies=False,
                    reason="seed below min_support (distinct sequence identities)",
                )

        self._results.extend(frontier)
        self._grow(frontier, depth + 1)

        self._logger.step(
            "mining-complete",
            patterns=len(self._results),
            max_depth_reached=c.max_pattern_length,
        )
        return self._results

    def _grow(self, current: list[RawPattern], depth: int) -> None:
        if depth >= self._c.max_pattern_length:
            return
        c = self._c
        # Extend only with symbols already frequent at length 1: any pattern
        # symbol must itself be frequent, so this is a safe vocabulary cut.
        frequent_symbols = [r.pattern[0] for r in self._results if len(r.pattern) == 1]
        next_level: list[RawPattern] = []
        for node in current:
            for symbol in frequent_symbols:
                supporting: dict[str, frozenset[int]] = {}
                per_seq_evidence: dict[str, int] = {}
                for data in self._sequences:
                    old_starts = node.supporting.get(data.sequence_id)
                    if old_starts is None:
                        continue
                    proj = _project(data, old_starts, symbol, c)
                    if proj:
                        supporting[data.sequence_id] = proj
                        per_seq_evidence[data.sequence_id] = len(proj)
                support = len(supporting)
                if support >= c.min_support:
                    self._logger.decision(
                        "pattern-extend",
                        pattern=list(node.pattern) + [symbol],
                        support=support,
                        suffix_starts_per_sequence=per_seq_evidence,
                        qualifies=True,
                    )
                    next_level.append(
                        RawPattern(node.pattern + (symbol,), supporting)
                    )
                else:
                    self._logger.decision(
                        "pattern-pruned",
                        pattern=list(node.pattern) + [symbol],
                        support=support,
                        min_support=c.min_support,
                        qualifies=False,
                        reason="below min_support (distinct sequence identities)",
                    )
        self._results.extend(next_level)
        if next_level:
            self._grow(next_level, depth + 1)
