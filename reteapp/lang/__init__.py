"""Rule language layer: model, parser and compiler.

This package contains *no* matching logic. It turns JSON rule documents into
validated, immutable intermediate representations consumed by
:mod:`reteapp.core`.
"""

from .compiler import CompiledRule, compile_rules
from .model import (
    Action,
    AssertTemplate,
    Binding,
    ConditionalElement,
    Constraint,
    LiteralConstraint,
    Rule,
    VariableConstraint,
)
from .parser import RuleParseError, parse_rules, parse_rules_json

__all__ = [
    "Action",
    "AssertTemplate",
    "Binding",
    "CompiledRule",
    "ConditionalElement",
    "Constraint",
    "LiteralConstraint",
    "Rule",
    "RuleParseError",
    "VariableConstraint",
    "compile_rules",
    "parse_rules",
    "parse_rules_json",
]
