"""Rule language: relational-algebra AST, JSON parser and schema validator.

Supported positive-relational operators:

* ``relation``  - base table scan (optional ``alias`` for self-joins)
* ``select``    - selection (filter)
* ``project``   - projection (column list)
* ``join``      - natural-style inner equi-join on ``on`` column pairs
* ``union``     - bag union (set-compatible inputs only)

The parser resolves every column reference into a canonical qualified label
``alias.column`` and attaches the output label tuple to each node, so the
execution engine never does name resolution.

NULL support scope (enforced here and honored by the engine):

* NULLs are stored and pass through projection/joins as values.
* A NULL never satisfies a comparison predicate (SQL three-valued semantics:
  the predicate evaluates to UNKNOWN and the row is dropped).
* In a join/equality, NULL never matches anything, including another NULL.
* ``is_null`` / ``is_not_null`` are the only NULL-aware predicates.
* Writing a comparison against a JSON ``null`` literal is rejected, because
  ``col = NULL`` never holds and usually indicates a mistake.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# ---- error categories -----------------------------------------------------

CATEGORY_MALFORMED = "MALFORMED_QUERY"
CATEGORY_UNKNOWN_RELATION = "UNKNOWN_RELATION"
CATEGORY_UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
CATEGORY_AMBIGUOUS_COLUMN = "AMBIGUOUS_COLUMN"
CATEGORY_UNSUPPORTED_OPERATOR = "UNSUPPORTED_OPERATOR"
CATEGORY_NULL_PREDICATE = "NULL_PREDICATE_NOT_SUPPORTED"
CATEGORY_UNION_MISMATCH = "UNION_SCHEMA_MISMATCH"
CATEGORY_DUPLICATE_COLUMN = "DUPLICATE_OUTPUT_COLUMN"
CATEGORY_TOO_COMPLEX = "QUERY_TOO_COMPLEX"

COMPARISON_OPS = frozenset({"=", "!=", "<", "<=", ">", ">="})
NULL_OPS = frozenset({"is_null", "is_not_null"})
ROOT_OPS = frozenset({"relation", "select", "project", "join", "union"})


class QueryValidationError(ValueError):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(f"[{category}] {message}")
        self.category = category
        self.message = message


# ---- AST -------------------------------------------------------------------


@dataclass(frozen=True)
class RelationScan:
    relation: str
    alias: str
    output_labels: tuple[str, ...]
    kind: str = "relation"


@dataclass(frozen=True)
class Predicate:
    label: str
    op: str
    value: Any = None  # unset for is_null / is_not_null


@dataclass(frozen=True)
class Select:
    input: Any
    predicate: Predicate
    output_labels: tuple[str, ...]
    kind: str = "select"


@dataclass(frozen=True)
class Project:
    input: Any
    columns: tuple[str, ...]
    output_labels: tuple[str, ...]
    kind: str = "project"


@dataclass(frozen=True)
class Join:
    left: Any
    right: Any
    on: tuple[tuple[str, str], ...]
    output_labels: tuple[str, ...]
    kind: str = "join"


@dataclass(frozen=True)
class Union:
    left: Any
    right: Any
    output_labels: tuple[str, ...]
    kind: str = "union"


Node = RelationScan | Select | Project | Join | Union


# ---- parser ----------------------------------------------------------------


class _Parser:
    def __init__(self, schema: Mapping[str, tuple[str, ...]], max_nodes: int) -> None:
        self.schema = dict(schema)
        self.max_nodes = max_nodes
        self.node_count = 0

    def parse(self, query: Any) -> Node:
        node = self._parse_node(query)
        if self.node_count > self.max_nodes:
            raise QueryValidationError(
                CATEGORY_TOO_COMPLEX,
                f"query has {self.node_count} nodes; limit is {self.max_nodes}",
            )
        return node

    def _parse_node(self, query: Any) -> Node:
        if not isinstance(query, Mapping):
            raise QueryValidationError(
                CATEGORY_MALFORMED, "query node must be an object with an 'op'"
            )
        op = query.get("op")
        if op not in ROOT_OPS:
            raise QueryValidationError(
                CATEGORY_UNSUPPORTED_OPERATOR,
                f"operator {op!r} is not supported; expected one of "
                f"{sorted(ROOT_OPS)}",
            )
        self.node_count += 1
        handler = {
            "relation": self._parse_relation,
            "select": self._parse_select,
            "project": self._parse_project,
            "join": self._parse_join,
            "union": self._parse_union,
        }[op]
        return handler(query)

    def _parse_relation(self, query: Mapping) -> RelationScan:
        name = query.get("relation")
        if not isinstance(name, str) or not name:
            raise QueryValidationError(
                CATEGORY_MALFORMED, "'relation' must name a base table"
            )
        if name not in self.schema:
            raise QueryValidationError(
                CATEGORY_UNKNOWN_RELATION,
                f"relation {name!r} is not present in this input version",
            )
        alias = query.get("alias", name)
        if not isinstance(alias, str) or not alias:
            raise QueryValidationError(
                CATEGORY_MALFORMED, "'alias' must be a non-empty string"
            )
        labels = tuple(f"{alias}.{col}" for col in self.schema[name])
        return RelationScan(relation=name, alias=alias, output_labels=labels)

    def _parse_select(self, query: Mapping) -> Select:
        child = self._require_child(query)
        condition = query.get("condition")
        if not isinstance(condition, Mapping):
            raise QueryValidationError(
                CATEGORY_MALFORMED, "'select' requires a 'condition' object"
            )
        predicate = self._parse_predicate(condition, child.output_labels)
        return Select(
            input=child, predicate=predicate, output_labels=child.output_labels
        )

    def _parse_predicate(self, condition: Mapping, labels: tuple[str, ...]) -> Predicate:
        col = condition.get("col")
        op = condition.get("op")
        if not isinstance(col, str) or not isinstance(op, str):
            raise QueryValidationError(
                CATEGORY_MALFORMED, "condition requires string 'col' and 'op'"
            )
        label = self._resolve_label(col, labels)
        if op in NULL_OPS:
            return Predicate(label=label, op=op)
        if op not in COMPARISON_OPS:
            raise QueryValidationError(
                CATEGORY_UNSUPPORTED_OPERATOR,
                f"comparison operator {op!r} is not supported; expected one of "
                f"{sorted(COMPARISON_OPS | NULL_OPS)}",
            )
        if "value" not in condition:
            raise QueryValidationError(
                CATEGORY_MALFORMED, f"condition with op {op!r} requires 'value'"
            )
        value = condition["value"]
        if value is None:
            raise QueryValidationError(
                CATEGORY_NULL_PREDICATE,
                f"comparison {col} {op} NULL can never hold under SQL semantics; "
                "use op 'is_null' / 'is_not_null' instead",
            )
        self._check_comparable_types(value)
        return Predicate(label=label, op=op, value=value)

    @staticmethod
    def _check_comparable_types(value: Any) -> None:
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise QueryValidationError(
                CATEGORY_MALFORMED,
                f"comparison value must be a string or number, got {type(value).__name__}",
            )

    def _parse_project(self, query: Mapping) -> Project:
        child = self._require_child(query)
        columns = query.get("columns")
        if not isinstance(columns, list) or not columns:
            raise QueryValidationError(
                CATEGORY_MALFORMED, "'project' requires a non-empty 'columns' list"
            )
        resolved: list[str] = []
        for col in columns:
            if not isinstance(col, str):
                raise QueryValidationError(
                    CATEGORY_MALFORMED, "project columns must be strings"
                )
            label = self._resolve_label(col, child.output_labels)
            if label in resolved:
                raise QueryValidationError(
                    CATEGORY_DUPLICATE_COLUMN,
                    f"projected column {label!r} appears more than once",
                )
            resolved.append(label)
        return Project(
            input=child,
            columns=tuple(resolved),
            output_labels=tuple(resolved),
        )

    def _parse_join(self, query: Mapping) -> Join:
        left_raw = query.get("left")
        right_raw = query.get("right")
        if left_raw is None or right_raw is None:
            raise QueryValidationError(
                CATEGORY_MALFORMED, "'join' requires 'left' and 'right' inputs"
            )
        left = self._parse_node(left_raw)
        right = self._parse_node(right_raw)
        on = query.get("on")
        if not isinstance(on, list) or not on:
            raise QueryValidationError(
                CATEGORY_MALFORMED,
                "'join' requires a non-empty 'on' list of [leftCol, rightCol] pairs",
            )
        pairs: list[tuple[str, str]] = []
        for pair in on:
            if not isinstance(pair, list) or len(pair) != 2:
                raise QueryValidationError(
                    CATEGORY_MALFORMED, "each join 'on' entry must be [left, right]"
                )
            left_label = self._resolve_label(pair[0], left.output_labels)
            right_label = self._resolve_label(pair[1], right.output_labels)
            pairs.append((left_label, right_label))
        overlap = set(left.output_labels) & set(right.output_labels)
        if overlap:
            # Two branches emitting the same qualified label make result rows
            # ambiguous; require aliases to disambiguate.
            raise QueryValidationError(
                CATEGORY_AMBIGUOUS_COLUMN,
                f"join branches share output labels {sorted(overlap)}; "
                "give the scans distinct aliases",
            )
        return Join(
            left=left,
            right=right,
            on=tuple(pairs),
            output_labels=left.output_labels + right.output_labels,
        )

    def _parse_union(self, query: Mapping) -> Union:
        left_raw = query.get("left")
        right_raw = query.get("right")
        if left_raw is None or right_raw is None:
            raise QueryValidationError(
                CATEGORY_MALFORMED, "'union' requires 'left' and 'right' inputs"
            )
        left = self._parse_node(left_raw)
        right = self._parse_node(right_raw)
        # Set-compatible: same arity, and base column names match positionally
        # (relation aliases are allowed to differ, like SQL table aliases).
        left_base = tuple(label.rsplit(".", 1)[-1] for label in left.output_labels)
        right_base = tuple(label.rsplit(".", 1)[-1] for label in right.output_labels)
        if left_base != right_base:
            raise QueryValidationError(
                CATEGORY_UNION_MISMATCH,
                f"union inputs must have identical columns in the same order; "
                f"left={list(left.output_labels)} right={list(right.output_labels)}",
            )
        # Result columns take the left branch's qualified labels positionally.
        return Union(
            left=left, right=right, output_labels=left.output_labels
        )

    def _require_child(self, query: Mapping) -> Node:
        raw = query.get("input")
        if raw is None:
            raise QueryValidationError(
                CATEGORY_MALFORMED, f"operator {query.get('op')!r} requires 'input'"
            )
        return self._parse_node(raw)

    @staticmethod
    def _resolve_label(ref: str, labels: tuple[str, ...]) -> str:
        if ref in labels:
            return ref
        # bare (unqualified) reference: resolve by suffix ".ref"
        if "." not in ref:
            matches = [label for label in labels if label.endswith(f".{ref}")]
            if not matches:
                raise QueryValidationError(
                    CATEGORY_UNKNOWN_COLUMN,
                    f"column {ref!r} does not exist; available: {list(labels)}",
                )
            if len(matches) > 1:
                raise QueryValidationError(
                    CATEGORY_AMBIGUOUS_COLUMN,
                    f"column {ref!r} is ambiguous; qualify it as one of {matches}",
                )
            return matches[0]
        raise QueryValidationError(
            CATEGORY_UNKNOWN_COLUMN,
            f"column {ref!r} does not exist; available: {list(labels)}",
        )


def parse_query(
    query: Any,
    schema: Mapping[str, tuple[str, ...]],
    max_nodes: int = 200,
) -> Node:
    """Parse and validate a JSON-specified relational query against a schema."""
    return _Parser(schema, max_nodes).parse(query)
