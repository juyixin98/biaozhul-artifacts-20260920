"""Shared pytest helpers: load fixtures without going through the kernel."""

from __future__ import annotations

import json
import os
from pathlib import Path

from min_iowl.lang.parser import axiom_from_json, parse_axioms
from min_iowl.lang import ast

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"


def load_json_fixture(name: str) -> dict:
    with open(DATA / name, encoding="utf-8") as fh:
        return json.load(fh)


def fixture_items(name: str) -> list[tuple[str, ast.Axiom]]:
    data = load_json_fixture(name)
    return [(f"a{i + 1:03d}", axiom_from_json(a, i)) for i, a in enumerate(data["axioms"])]


def functional_items(text: str) -> list[tuple[str, ast.Axiom]]:
    return [(f"a{i + 1:03d}", a) for i, a in enumerate(parse_axioms(text))]
