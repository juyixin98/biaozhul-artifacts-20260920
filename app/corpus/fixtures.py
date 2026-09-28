"""Local synthetic fixtures.

No external accounts or real business data: every corpus here is hand-designed
to exercise a specific semantic edge.  The expected mining outcomes are
documented per fixture and cross-checked independently in
``tests/test_fixtures_expectations.py``.

Fixture catalogue
-----------------
``repeated_symbols``
    Sequences contain the same symbol many times.  Support must count a
    sequence *once* regardless of how many embeddings a pattern has there.

``time_ties``
    Equal timestamps between adjacent events.  Time gap of 0 must accept
    same-timestamp ordered pairs; the position gap still applies independently.

``multiple_embeddings``
    One sequence admits several embeddings of the same pattern, including a
    tempting "first match" that must NOT be kept because it prunes a valid
    alternative suffix that a later pattern symbol needs.

``gap_boundaries``
    Hand-built so that gaps land exactly on and one beyond the constraint
    boundary, for both position and time gaps (declared independently).
"""
from __future__ import annotations

from app.models import CorpusCreateRequest, EventIn, SequenceIn


def _seq(seq_id: str, events: list[tuple[str, float | None]]) -> SequenceIn:
    return SequenceIn(
        sequence_id=seq_id,
        events=[EventIn(symbol=s, timestamp=t) for s, t in events],
    )


def repeated_symbols() -> CorpusCreateRequest:
    # A appears 4 times in s1, but pattern <A> still has support 1 there.
    # Pattern <A,B> embeds twice in s1 and once in s2 -> support {s1,s2} = 2.
    return CorpusCreateRequest(
        name="repeated_symbols",
        description="Repeated symbols must not inflate sequence-based support",
        sequences=[
            _seq("s1", [("A", 1), ("A", 2), ("B", 3), ("A", 4), ("B", 5)]),
            _seq("s2", [("A", 1), ("B", 2), ("C", 3)]),
            _seq("s3", [("B", 1), ("A", 2), ("C", 3)]),  # B before A: no <A,B>
        ],
    )


def time_ties() -> CorpusCreateRequest:
    # s1: A@10, B@10 (tie), C@20
    # s2: A@10, B@10 (tie), C@10 (triple tie)
    # s3: A@10, B@11 (gap 1), C@20
    return CorpusCreateRequest(
        name="time_ties",
        description="Equal timestamps are legal ordered events",
        sequences=[
            _seq("s1", [("A", 10), ("B", 10), ("C", 20)]),
            _seq("s2", [("A", 10), ("B", 10), ("C", 10)]),
            _seq("s3", [("A", 10), ("B", 11), ("C", 20)]),
        ],
    )


def multiple_embeddings() -> CorpusCreateRequest:
    # s1: A B A C B C
    #   <A,B> embeddings: (0,1), (0,4), (2,4)
    #   <A,B,C> embeddings: (0,1,3),(0,1,5),(0,4,5),(2,4,5)
    #   Greedy first-match for <A,B> then C still works here, but:
    # s2: A A B B
    #   <A,B> embeddings: (0,2),(0,3),(1,2),(1,3) — projections must retain
    #   the second A (position 1), otherwise patterns extending via the later
    #   region are not lost; both As are kept as first-column pseudos.
    # s3: A B A B C
    #   <A,B,C> embeddings: (0,1,4), (0,3,4), (2,3,4) -> one sequence, support 1
    return CorpusCreateRequest(
        name="multiple_embeddings",
        description="Multiple embeddings per sequence must all be retained",
        sequences=[
            _seq("s1", [("A", 1), ("B", 2), ("A", 3), ("C", 4), ("B", 5), ("C", 6)]),
            _seq("s2", [("A", 1), ("A", 2), ("B", 3), ("B", 4)]),
            _seq("s3", [("A", 1), ("B", 2), ("A", 3), ("B", 4), ("C", 5)]),
        ],
    )


def gap_boundaries() -> CorpusCreateRequest:
    # Position-gap boundary (index distance, adjacent = 1):
    #   s1: A . B   -> gap 2 (one event between)
    #   s2: A . . B -> gap 3 (two events between)
    # Time-gap boundary (independent of position gap):
    #   s3: A@0 B@5  -> time gap exactly 5
    #   s4: A@0 B@6  -> time gap 6
    return CorpusCreateRequest(
        name="gap_boundaries",
        description="Exact boundary cases for position and time gaps",
        sequences=[
            _seq("s1", [("A", 0), ("x", 1), ("B", 2)]),
            _seq("s2", [("A", 0), ("x", 1), ("y", 2), ("B", 3)]),
            _seq("s3", [("A", 0.0), ("B", 5.0)]),
            _seq("s4", [("A", 0.0), ("B", 6.0)]),
        ],
    )


def pruning_trap() -> CorpusCreateRequest:
    """The canonical "don't prune alternative embeddings" trap.

    Sequence ``trap``:  A(0)  C(1)  A(2)  B(3)

    With ``max_gap_position = 1`` the pattern ``<A,B>`` has exactly one
    embedding, (2,3): the early A at position 0 cannot reach B at 3 within the
    gap (distance 3), but the *later* A at position 2 can.  A greedy engine
    that binds A to its first occurrence and stops would wrongly report the
    pattern absent.  Retaining every A occurrence as a projector keeps the
    alternative embedding alive.  With no gap limit the same sequence also
    admits (0,3), so the fixture contrasts tight-gap vs unbounded behavior.
    """
    return CorpusCreateRequest(
        name="pruning_trap",
        description="Alternative embeddings must survive projection",
        sequences=[
            _seq("trap", [("A", 1), ("C", 2), ("A", 3), ("B", 4)]),
            _seq("ok", [("A", 1), ("A", 2), ("B", 3)]),
        ],
    )


FIXTURES: dict[str, callable] = {
    "repeated_symbols": repeated_symbols,
    "time_ties": time_ties,
    "multiple_embeddings": multiple_embeddings,
    "gap_boundaries": gap_boundaries,
    "pruning_trap": pruning_trap,
}


def all_fixture_requests() -> dict[str, CorpusCreateRequest]:
    return {name: factory() for name, factory in FIXTURES.items()}
