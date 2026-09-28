"""Problem definition loading from YAML or JSON files/strings.

All external problem content is validated at the system boundary. Parse
errors and schema errors are both reported as ``INVALID_INPUT`` with the
source identity attached, so a failing test run can be correlated with
the exact input file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .errors import ValidationFailure
from .models import Problem


def parse_problem_dict(payload: dict[str, Any], *, source: str = "<dict>") -> Problem:
    if not isinstance(payload, dict):
        raise ValidationFailure("problem definition must be a mapping", source=source)
    try:
        return Problem.model_validate(payload)
    except ValidationFailure:
        raise
    except (ValidationError, ValueError) as exc:
        raise ValidationFailure(
            f"invalid problem definition from {source}: {exc}",
            source=source,
        ) from exc


def loads(text: str, *, source: str = "<string>") -> Problem:
    """Parse YAML or JSON text (YAML is a superset of JSON here)."""
    try:
        payload = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationFailure(f"YAML/JSON parse error in {source}: {exc}", source=source) from exc
    return parse_problem_dict(payload, source=source)


def load_file(path: str | Path) -> Problem:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationFailure(f"cannot read problem file {path}: {exc}", source=str(path)) from exc
    if path.suffix.lower() == ".json":
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValidationFailure(
                f"JSON parse error in {path}: line {exc.lineno} column {exc.colno}: {exc.msg}",
                source=str(path),
                line=exc.lineno,
            ) from exc
        return parse_problem_dict(payload, source=str(path))
    return loads(text, source=str(path))
