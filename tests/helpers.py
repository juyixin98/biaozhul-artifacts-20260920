"""Helpers shared by the independent test-suite."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from reteapp.core.engine import Engine
from reteapp.lang import compile_rules, parse_rules_json

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES / name).open("r", encoding="utf-8") as fh:
        return json.load(fh)


def build_engine(
    document: dict[str, Any] | list[dict[str, Any]],
    *,
    max_fire_rounds: int = 100,
    duplicate_policy: str = "multiset",
    refraction: bool | None = None,
) -> tuple[Engine, list[Any]]:
    """Compile independently twice: Rule objects for the oracle, CompiledRule
    for the engine, from the same source document."""

    rules = parse_rules_json(document)
    if refraction is not None:
        from dataclasses import replace

        rules = [replace(r, refraction=refraction) for r in rules]
    compiled = compile_rules(parse_rules_json(document))
    if refraction is not None:
        compiled = [
            type(c)(
                name=c.name,
                salience=c.salience,
                enabled=c.enabled,
                refraction=refraction,
                plans=c.plans,
                action=c.action,
                binding_origins=c.binding_origins,
            )
            for c in compiled
        ]
    engine = Engine(
        compiled,
        max_fire_rounds=max_fire_rounds,
        duplicate_policy=duplicate_policy,
    )
    return engine, rules
