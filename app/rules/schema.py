"""Rule language: schema and validation for Horn-style justifications.

A rule has the shape::

    rule r1: A, B => C

meaning "if nodes A and B both hold, node C holds". The consequent may be
the contradiction node ``⊥``; environments that derive it become nogoods.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field, field_validator

from ..core.types import CONTRADICTION, Rule

_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")


class RuleSpec(BaseModel):
    """Wire/validation format for one rule."""

    rule_id: str = Field(min_length=1, max_length=64)
    antecedents: list[str] = Field(default_factory=list, max_length=32)
    consequent: str = Field(min_length=1, max_length=64)

    @field_validator("rule_id", "consequent")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        if value != CONTRADICTION and not _NAME_RE.match(value):
            raise ValueError(f"invalid symbol name: {value!r}")
        return value

    @field_validator("antecedents")
    @classmethod
    def _well_formed_antecedents(cls, value: list[str]) -> list[str]:
        for name in value:
            if not _NAME_RE.match(name):
                raise ValueError(f"invalid antecedent name: {name!r}")
        if len(set(value)) != len(value):
            raise ValueError("duplicate antecedents")
        return value

    def to_rule(self) -> Rule:
        return Rule(
            rule_id=self.rule_id,
            antecedents=tuple(self.antecedents),
            consequent=self.consequent,
        )


class AssumptionSpec(BaseModel):
    name: str = Field(min_length=1, max_length=64)

    @field_validator("name")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        if value == CONTRADICTION or not _NAME_RE.match(value):
            raise ValueError(f"invalid assumption name: {value!r}")
        return value


class PremiseSpec(BaseModel):
    node: str = Field(min_length=1, max_length=64)

    @field_validator("node")
    @classmethod
    def _well_formed(cls, value: str) -> str:
        if value == CONTRADICTION or not _NAME_RE.match(value):
            raise ValueError(f"invalid premise name: {value!r}")
        return value
