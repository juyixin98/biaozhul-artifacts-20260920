"""Reviewable unified-diff rendering for audit findings."""
from __future__ import annotations

import difflib
import json
from typing import Any


def json_diff(path: str, expected: Any, actual: Any) -> str:
    """Render a unified diff between expected and actual JSON values."""
    exp = json.dumps(expected, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    act = json.dumps(actual, indent=2, sort_keys=True, ensure_ascii=False).splitlines()
    diff = difflib.unified_diff(
        exp, act,
        fromfile=f"expected:{path}",
        tofile=f"actual:{path}",
        lineterm="",
    )
    return "\n".join(diff)


def text_diff(path: str, expected: str, actual: str) -> str:
    diff = difflib.unified_diff(
        expected.splitlines(),
        actual.splitlines(),
        fromfile=f"expected:{path}",
        tofile=f"actual:{path}",
        lineterm="",
    )
    return "\n".join(diff)
