"""End-to-end service tests: real scoring pipeline + constraints + locks."""

from __future__ import annotations

import pytest

from entity_resolution.clustering import SolverConfig
from entity_resolution.errors import (
    ConstraintConflictError,
    EmptyCorpusError,
    InvalidRequestError,
    ResourceExhaustedError,
)
from entity_resolution.models import CorpusIn, LinkIn
from entity_resolution.service import EntityResolutionService
from entity_resolution.similarity import SimilarityConfig
from entity_resolution.storage import Storage

# Real-text chain: A,B near-duplicate; B,C share a token but are different
# entities. At threshold 0.45 all three pairs are candidates, yet the global
# optimum (and the A-C cannot-link) keeps C separate.
CHAIN = [
    {"id": "A", "name": "Pioneer Corp"},
    {"id": "B", "name": "Pioneer Corporation"},
    {"id": "C", "name": "Pioneer Foods"},
]


def _cluster_map(solution) -> dict[str, frozenset[str]]:
    return {c.cluster_id: frozenset(c.members) for c in solution.clusters}


def test_chain_is_split_not_threshold_transitive(service: EntityResolutionService) -> None:
    service.load_corpus(CorpusIn(records=CHAIN, cannot_links=[("A", "C")]))
    solution = service.resolve(0.45)
    groups = set(_cluster_map(solution).values())
    assert frozenset({"A", "B"}) in groups
    assert frozenset({"C"}) in groups
    # Rejected-pair reasons are specific, not a generic failure.
    reasons = {tuple(rp["pair"]): rp["reason"] for rp in solution.rejected_pairs}
    assert reasons[("A", "C")] == "cannot_link"
    assert reasons[("B", "C")] == "global_objective_split"
    # Evidence travels with the output cluster.
    ab = next(c for c in solution.clusters if set(c.members) == {"A", "B"})
    assert ab.evidence and ab.evidence[0].score >= 0.7


def test_chain_split_even_without_cannot_link(service: EntityResolutionService) -> None:
    service.load_corpus(CorpusIn(records=CHAIN))
    solution = service.resolve(0.45)
    groups = set(_cluster_map(solution).values())
    # The objective alone breaks transitivity: C is not pulled in.
    assert frozenset({"C"}) in groups


def test_cross_language_alias_records_merge(service: EntityResolutionService) -> None:
    corpus = CorpusIn(
        records=[
            {"id": "g1", "name": "Gazprom", "language": "en"},
            {"id": "g2", "name": "Газпром", "language": "ru"},
            {"id": "r1", "name": "Rosneft", "language": "en"},
        ],
        aliases={"Gazprom": ["Газпром"]},
    )
    service.load_corpus(corpus)
    solution = service.resolve(0.6)
    groups = set(_cluster_map(solution).values())
    assert frozenset({"g1", "g2"}) in groups
    assert frozenset({"r1"}) in groups


def test_same_name_distinct_identifier_split(strict_service: EntityResolutionService) -> None:
    corpus = CorpusIn(
        records=[
            {"id": "x1", "name": "Acme Bank", "attributes": {"reg_id": "111"}},
            {"id": "x2", "name": "Acme Bank", "attributes": {"reg_id": "222"}},
        ]
    )
    strict_service.load_corpus(corpus)
    solution = strict_service.resolve(0.6)
    groups = set(_cluster_map(solution).values())
    # Same display name, conflicting hard identifier -> different entities.
    assert groups == {frozenset({"x1"}), frozenset({"x2"})}


def test_lock_is_preserved_and_reported(service: EntityResolutionService, tmp_path) -> None:
    service.load_corpus(CorpusIn(records=CHAIN, cannot_links=[("A", "C")]))
    service.resolve(0.45)
    locked = service.lock_cluster("lock-ab", ["A", "B"])
    assert locked["locked"] is True

    # The lock itself is a reported change (latest run is the lock run).
    affected = service.affected_entities()
    assert affected.after["A"] == "lock-ab"
    assert affected.after["B"] == "lock-ab"

    # A subsequent resolve preserves the human-confirmed mapping.
    solution = service.resolve(0.45)
    by_members = {frozenset(c.members): c for c in solution.clusters}
    ab = by_members[frozenset({"A", "B"})]
    assert ab.locked is True
    assert ab.cluster_id == "lock-ab"


def test_add_link_contradiction_is_state_conflict(service: EntityResolutionService) -> None:
    service.load_corpus(
        CorpusIn(
            records=CHAIN,
            must_links=[("A", "B"), ("B", "C")],
        )
    )
    with pytest.raises(ConstraintConflictError) as exc:
        service.add_link(LinkIn(left="A", right="C", kind="cannot"))
    assert exc.value.category == "STATE_CONFLICT"
    assert exc.value.details["reason"] == "must_link_closure"


def test_empty_corpus_is_input_error(service: EntityResolutionService) -> None:
    with pytest.raises(EmptyCorpusError) as exc:
        service.load_corpus(CorpusIn(records=[]))
    assert exc.value.category == "INPUT_ERROR"


def test_duplicate_ids_are_input_error(service: EntityResolutionService) -> None:
    with pytest.raises(InvalidRequestError) as exc:
        service.load_corpus(CorpusIn(records=CHAIN[:2] + [CHAIN[0]]))
    assert exc.value.category == "INPUT_ERROR"
    assert "A" in exc.value.details["duplicates"]


def test_resource_exhausted_distinct_from_computation_failure(paths) -> None:
    storage = Storage(paths / "big.sqlite3")
    svc = EntityResolutionService(
        storage,
        similarity=SimilarityConfig(threshold=0.99),
        solver=SolverConfig(mode="exact", max_exact_partitions=100),
        log_dir=paths / "logs",
    )
    records = [{"id": f"n{i}", "name": f"Unique Name Number {i}"} for i in range(9)]
    svc.load_corpus(CorpusIn(records=records))
    with pytest.raises(ResourceExhaustedError) as exc:
        svc.resolve()
    assert exc.value.category == "RESOURCE_EXHAUSTED"
    assert exc.value.details["blocks"] == 9
