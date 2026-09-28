"""Rule language: problem schema and validation.

A problem is a finite-domain CSP over integer values:
- variables with explicit finite domains (non-empty lists of distinct ints;
  an empty domain is rejected at the boundary, see README "Boundary semantics")
- constraints:
    - all_different over >= 2 variables (global constraint, Régin filtering)
    - table: binary relation given as the explicit list of allowed pairs
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class Variable(BaseModel):
    name: str = Field(min_length=1)
    domain: list[int] = Field(min_length=1)

    @field_validator("domain")
    @classmethod
    def _distinct(cls, values: list[int]) -> list[int]:
        if len(set(values)) != len(values):
            raise ValueError("domain values must be distinct")
        return values


class AllDifferentConstraint(BaseModel):
    type: Literal["all_different"]
    id: str = Field(min_length=1)
    vars: list[str] = Field(min_length=2)


class TableConstraint(BaseModel):
    """Binary relation: allowed pairs over (vars[0], vars[1])."""

    type: Literal["table"]
    id: str = Field(min_length=1)
    vars: list[str]
    allowed: list[list[int]]

    @field_validator("vars")
    @classmethod
    def _binary(cls, vars: list[str]) -> list[str]:
        if len(vars) != 2:
            raise ValueError("table constraint must be binary (exactly 2 vars)")
        return vars

    @field_validator("allowed")
    @classmethod
    def _pairs(cls, allowed: list[list[int]]) -> list[list[int]]:
        for pair in allowed:
            if len(pair) != 2:
                raise ValueError("each allowed entry must be a pair [a, b]")
        return allowed


Constraint = Annotated[
    Union[AllDifferentConstraint, TableConstraint], Field(discriminator="type")
]


class Problem(BaseModel):
    name: str = Field(min_length=1)
    variables: list[Variable] = Field(min_length=1)
    constraints: list[Constraint] = []

    @model_validator(mode="after")
    def _check(self) -> "Problem":
        names = [v.name for v in self.variables]
        if len(set(names)) != len(names):
            raise ValueError("variable names must be unique")
        known = set(names)
        domains = {v.name: set(v.domain) for v in self.variables}
        ids: set[str] = set()
        for c in self.constraints:
            if c.id in ids:
                raise ValueError(f"duplicate constraint id: {c.id}")
            ids.add(c.id)
            if len(set(c.vars)) != len(c.vars):
                raise ValueError(f"constraint {c.id} lists a variable more than once")
            for var in c.vars:
                if var not in known:
                    raise ValueError(f"constraint {c.id} references unknown variable {var}")
            if isinstance(c, TableConstraint):
                a, b = c.vars
                for x, y in c.allowed:
                    if x not in domains[a] or y not in domains[b]:
                        raise ValueError(
                            f"constraint {c.id}: allowed pair [{x}, {y}] outside domains"
                        )
        return self

    def initial_domains(self) -> dict[str, frozenset[int]]:
        return {v.name: frozenset(v.domain) for v in self.variables}
