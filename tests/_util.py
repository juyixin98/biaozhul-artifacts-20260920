"""Helpers for tests."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from resflow.analyzer import analyze_source  # noqa: E402
from resflow.errors import ResflowError  # noqa: E402


def analyze(src: str, **kw):
    return analyze_source(src, filename="test.rf", **kw)


def expect_error(src: str):
    """Analyze invalid source; returns the raised error (caller asserts)."""
    return analyze_source(src, filename="test.rf")


def codes(result) -> set:
    return {f["code"] for f in result["findings"]}


def function(result, name="main"):
    for fn in result["functions"]:
        if fn["name"] == name:
            return fn
    raise KeyError(name)


def path_kinds(result, name="main"):
    return [p["kind"] for p in function(result, name)["paths"]]
