"""Smoke test for the ASGI entry point module."""

from __future__ import annotations


def test_main_module_exposes_app() -> None:
    from app.main import app

    assert app is not None
    # The included router registers paths lazily in this FastAPI version, so
    # assert against the generated OpenAPI schema where all paths are present.
    paths = set(app.openapi()["paths"].keys())
    assert "/certify" in paths
    assert "/health" in paths
