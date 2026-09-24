"""Security guardrails: no code-execution primitives, no dangerous imports.

The interpreter is required to evaluate policies without ever executing
arbitrary code. These tests scan the shipped application code for the
standard execution/import primitives; any future change that introduces
one must update this test with a justification.
"""

import ast
from pathlib import Path

APP_DIR = Path(__file__).resolve().parents[1] / "app"

FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
FORBIDDEN_IMPORTS = {"pickle", "marshal", "subprocess", "importlib", "codeop"}


def _python_files():
    return [p for p in APP_DIR.rglob("*.py") if p.is_file()]


def test_no_dynamic_code_execution_calls():
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                name = None
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute) and fn.attr in FORBIDDEN_CALLS:
                    name = fn.attr
                if name in FORBIDDEN_CALLS:
                    offenders.append(f"{path.name}:{node.lineno} -> {name}")
    assert not offenders, f"forbidden execution primitives: {offenders}"


def test_no_dangerous_imports():
    offenders = []
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            else:
                continue
            for name in names:
                if name in FORBIDDEN_IMPORTS:
                    offenders.append(f"{path.name}:{node.lineno} -> {name}")
    assert not offenders, f"forbidden imports: {offenders}"


def test_operator_whitelist_is_closed():
    from app.interpreter import ALL_OPS

    # If a new operator is added it must be implemented in eval_value and
    # documented; this just pins the closed set so changes are deliberate.
    assert ALL_OPS == {
        "lit", "attr", "exists",
        "not", "and", "or",
        "eq", "ne", "lt", "lte", "gt", "gte",
        "in", "contains", "subset",
    }
