import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minidfa.config import Settings  # noqa: E402
from minidfa.service import DfaService  # noqa: E402


@pytest.fixture()
def settings(tmp_path: Path) -> Settings:
    return Settings(db_path=str(tmp_path / "test.db"), log_level="DEBUG")


@pytest.fixture()
def service(settings: Settings):
    svc = DfaService(settings)
    yield svc
    svc.close()
