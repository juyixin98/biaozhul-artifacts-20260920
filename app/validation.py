"""Template definition parsing and publish-time validation."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.constants import (
    NODE_APPROVAL,
    NODE_CONDITION,
    NODE_END,
    NODE_START,
    NODE_TYPES,
    SIGN_STRATEGIES,
)
from app.expressions import ConditionSyntaxError, parse_expression

# ---- Pydantic schema of the definition document ---------------------------
from pydantic import BaseModel, Field, model_validator


class ApprovalNode(BaseModel):
    id: str
    type: str = Field(pattern=f"^{NODE_APPROVAL}$")
    name: str | None = None
    strategy: str = Field(pattern=f"^({'|'.join(SIGN_STRATEGIES)})$")
    approvers: list[str] = Field(min_length=1)
    next_node: str
    timeout_seconds: int | None = Field(default=None, gt=0)
    escalation_target: str | None = None

    @model_validator(mode="after")
    def _check_escalation(self) -> "ApprovalNode":
        if (self.timeout_seconds is None) != (self.escalation_target is None):
            raise ValueError(
                "timeout_seconds and escalation_target must be set together"
            )
        if len(set(self.approvers)) != len(self.approvers):
            raise ValueError("approvers must be unique")
        if self.escalation_target is not None and (
            not self.escalation_target.strip()
            or self.escalation_target in self.approvers
        ):
            raise ValueError(
                "escalation_target must name a different approver (non-empty)"
            )
        return self


class ConditionBranch(BaseModel):
    expression: str
    next_node: str


class ConditionNode(BaseModel):
    id: str
    type: str = Field(pattern=f"^{NODE_CONDITION}$")
    name: str | None = None
    branches: list[ConditionBranch] = Field(min_length=1)
    default: str | None = None


class StartNode(BaseModel):
    id: str
    type: str = Field(pattern=f"^{NODE_START}$")
    name: str | None = None
    next_node: str


class EndNode(BaseModel):
    id: str
    type: str = Field(pattern=f"^{NODE_END}$")
    name: str | None = None


class TemplateDefinition(BaseModel):
    nodes: list[dict[str, Any]] = Field(min_length=1)


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str


def _node_ids(nodes: list[dict]) -> list[str]:
    return [n.get("id") for n in nodes]


def validate_definition(definition: dict[str, Any]) -> list[ValidationIssue]:
    """Return all validation issues; empty list means the definition is valid.

    Checks:
      * document shape and per-node schema (pydantic)
      * exactly one start node and at least one end node
      * unique node ids
      * every ``next_node`` / branch / default / escalation reference exists
      * every node reachable from start
      * no directed cycle (DFS)
      * every condition expression parses under the restricted grammar
    """
    issues: list[ValidationIssue] = []

    try:
        wrapper = TemplateDefinition.model_validate(definition)
    except Exception as exc:  # pydantic ValidationError
        return [ValidationIssue("schema", f"definition schema error: {exc}")]

    raw_nodes = wrapper.nodes

    # unique ids
    ids = _node_ids(raw_nodes)
    if len(set(ids)) != len(ids):
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        issues.append(ValidationIssue("duplicate_node", f"duplicate node ids: {dupes}"))

    id_set = {n["id"] for n in raw_nodes if isinstance(n.get("id"), str)}

    # typed parse per node (collects per-node schema errors)
    parsed: dict[str, BaseModel] = {}
    starts = 0
    ends = 0
    for raw in raw_nodes:
        node_type = raw.get("type")
        if node_type not in NODE_TYPES:
            issues.append(
                ValidationIssue(
                    "unknown_node_type",
                    f"node {raw.get('id')!r}: unknown type {node_type!r}",
                )
            )
            continue
        try:
            if node_type == NODE_START:
                parsed[raw["id"]] = StartNode.model_validate(raw)
                starts += 1
            elif node_type == NODE_APPROVAL:
                parsed[raw["id"]] = ApprovalNode.model_validate(raw)
            elif node_type == NODE_CONDITION:
                parsed[raw["id"]] = ConditionNode.model_validate(raw)
            else:
                parsed[raw["id"]] = EndNode.model_validate(raw)
                ends += 1
        except Exception as exc:
            issues.append(
                ValidationIssue("node_schema", f"node {raw.get('id')!r}: {exc}")
            )

    if starts != 1:
        issues.append(
            ValidationIssue("start_count", f"expected exactly 1 start node, got {starts}")
        )
    if ends < 1:
        issues.append(ValidationIssue("end_count", "at least one end node is required"))

    # reference checks + expression parsing
    edges: dict[str, list[str]] = {nid: [] for nid in parsed}
    for nid, node in parsed.items():
        if isinstance(node, (StartNode, ApprovalNode)):
            if node.next_node not in id_set:
                issues.append(
                    ValidationIssue(
                        "missing_reference",
                        f"node {nid!r} references missing node {node.next_node!r}",
                    )
                )
            else:
                edges[nid].append(node.next_node)
            # escalation_target names a *user*, not a node: its non-emptiness
            # and difference from approvers are checked by the node schema
        elif isinstance(node, ConditionNode):
            for branch in node.branches:
                if branch.next_node not in id_set:
                    issues.append(
                        ValidationIssue(
                            "missing_reference",
                            f"node {nid!r}: branch references missing node "
                            f"{branch.next_node!r}",
                        )
                    )
                else:
                    edges[nid].append(branch.next_node)
                try:
                    parse_expression(branch.expression)
                except ConditionSyntaxError as exc:
                    issues.append(
                        ValidationIssue(
                            "bad_expression",
                            f"node {nid!r}: {exc}",
                        )
                    )
            if node.default is not None:
                if node.default not in id_set:
                    issues.append(
                        ValidationIssue(
                            "missing_reference",
                            f"node {nid!r}: default references missing node "
                            f"{node.default!r}",
                        )
                    )
                else:
                    edges[nid].append(node.default)

    # Structural graph checks only make sense when start exists uniquely.
    start_id = next(
        (nid for nid, n in parsed.items() if isinstance(n, StartNode)), None
    )
    if start_id is not None and not any(i.code == "duplicate_node" for i in issues):
        reachable = _reachable_from(start_id, edges)
        unreachable = sorted(set(parsed) - reachable)
        if unreachable:
            issues.append(
                ValidationIssue(
                    "unreachable", f"unreachable nodes: {unreachable}"
                )
            )
        cycle = _find_cycle(edges, reachable)
        if cycle:
            issues.append(
                ValidationIssue(
                    "cycle", f"cycle detected involving nodes: {cycle}"
                )
            )

    return issues


def _reachable_from(start: str, edges: dict[str, list[str]]) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        stack.extend(edges.get(cur, []))
    return seen


def _find_cycle(
    edges: dict[str, list[str]], scope: set[str]
) -> list[str] | None:
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {nid: WHITE for nid in scope}
    stack: list[str] = []

    def visit(nid: str) -> list[str] | None:
        color[nid] = GRAY
        stack.append(nid)
        for nxt in edges.get(nid, []):
            if nxt not in scope:
                continue
            if color[nxt] == GRAY:
                start_idx = stack.index(nxt)
                return stack[start_idx:] + [nxt]
            if color[nxt] == WHITE:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        color[nid] = BLACK
        return None

    for nid in scope:
        if color[nid] == WHITE:
            found = visit(nid)
            if found:
                return found
    return None
