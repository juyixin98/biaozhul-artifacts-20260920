"""Application service: pins an input version, runs the query, and verifies.

This is the transactional boundary that guarantees answers and provenance come
from the *same* input version: a single version id is resolved once, the
schema, rows and weights are all read under that id, and the resolved id is
echoed back with the answer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from . import language as lang
from .engine import evaluate
from .expressions import MissingWeightError, Monomial, Polynomial
from .store import EvidenceStore, RelationData, VersionNotFound


class QueryError(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.message = message


@dataclass(frozen=True)
class ResolvedVersion:
    version_id: int
    schema: dict[str, tuple[str, ...]]
    relations: dict[str, RelationData]


class QueryService:
    def __init__(self, store: EvidenceStore, max_query_nodes: int = 200) -> None:
        self.store = store
        self.max_query_nodes = max_query_nodes

    # ---- version handling ---------------------------------------------

    def resolve_version(self, version_id: int | None) -> int:
        if version_id is not None:
            try:
                self.store.list_relations(version_id)
            except VersionNotFound as exc:
                raise QueryError(
                    "UNKNOWN_VERSION",
                    f"input version {version_id} does not exist",
                ) from exc
            return version_id
        latest = self.store.latest_version()
        if latest is None:
            raise QueryError(
                "NO_INPUT_VERSION",
                "no input version has been loaded; load fixtures first",
            )
        return latest

    def _load(self, version_id: int) -> ResolvedVersion:
        schema = self.store.fetch_schema(version_id)
        relations = {
            name: self.store.fetch_relation(version_id, name)
            for name in schema
        }
        return ResolvedVersion(
            version_id=version_id, schema=schema, relations=relations
        )

    # ---- query ---------------------------------------------------------

    def run(self, query: Any, version_id: int | None) -> dict:
        resolved_id = self.resolve_version(version_id)
        loaded = self._load(resolved_id)
        try:
            node = lang.parse_query(
                query, loaded.schema, max_nodes=self.max_query_nodes
            )
        except lang.QueryValidationError as exc:
            raise QueryError(exc.category, exc.message) from exc

        result = evaluate(node, loaded.relations)
        return {
            "version_id": resolved_id,
            "labels": list(result.labels),
            "rows": [
                {
                    "values": list(row.values),
                    "provenance": row.provenance.render(),
                    "expression": _serialize(row.provenance),
                }
                for row in result.rows
            ],
        }

    # ---- numeric verification -----------------------------------------

    def verify(self, version_id: int, rows: list[Mapping[str, Any]]) -> list[dict]:
        """Re-evaluate each row's polynomial with this version's weights.

        The polynomial is rebuilt from the caller-supplied serialized terms
        (the ones returned by :meth:`run`), so verification exercises the same
        expression the client received rather than a freshly computed one.
        """
        resolved_id = self.resolve_version(version_id)
        weights = self.store.fetch_weights(resolved_id)
        verified: list[dict] = []
        for row in rows:
            poly = _deserialize(row.get("expression"))
            try:
                numeric_value = poly.evaluate(weights)
            except MissingWeightError as exc:
                raise QueryError(
                    "MISSING_WEIGHT",
                    f"cannot verify: {exc}",
                ) from exc
            expected = row.get("expected_value")
            if expected is None:
                matches = True
            elif isinstance(expected, bool) or not isinstance(expected, (int, float)):
                raise QueryError(
                    "MALFORMED_EXPRESSION",
                    "'expected_value' must be a number when provided",
                )
            else:
                matches = math.isclose(numeric_value, float(expected), rel_tol=1e-9)
            entry = {
                "values": list(row.get("values", [])),
                "expression": poly.render(),
                "numeric_value": numeric_value,
                "matches": matches,
            }
            if expected is not None:
                entry["expected_value"] = float(expected)
            verified.append(entry)
        return verified


def _serialize(poly: Polynomial) -> dict:
    return {
        "terms": [
            {"witnesses": list(monomial), "coefficient": coeff}
            for monomial, coeff in poly.terms.items()
        ]
    }


def _deserialize(payload: Any) -> Polynomial:
    if not isinstance(payload, Mapping) or not isinstance(
        payload.get("terms"), list
    ):
        raise QueryError(
            "MALFORMED_EXPRESSION",
            "each row needs an 'expression' with a 'terms' list",
        )
    terms: dict[Monomial, int] = {}
    for term in payload["terms"]:
        if not isinstance(term, Mapping):
            raise QueryError("MALFORMED_EXPRESSION", "each term must be an object")
        witnesses = term.get("witnesses")
        coefficient = term.get("coefficient")
        if not isinstance(witnesses, list) or not all(
            isinstance(w, str) for w in witnesses
        ):
            raise QueryError(
                "MALFORMED_EXPRESSION", "term 'witnesses' must be a list of strings"
            )
        if not isinstance(coefficient, int) or coefficient <= 0:
            raise QueryError(
                "MALFORMED_EXPRESSION", "term 'coefficient' must be a positive int"
            )
        terms[tuple(witnesses)] = coefficient
    return Polynomial(terms)
