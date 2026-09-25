"""Shared pytest fixtures."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from enrange.store import ObjectStore  # noqa: E402


@pytest.fixture()
def store(tmp_path: Path) -> ObjectStore:
    return ObjectStore.open_or_create(tmp_path / "data")


@pytest.fixture()
def data_dir(tmp_path: Path) -> Path:
    return tmp_path / "data"
