import os
import sys
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

# Isolate runtime state before app.main is imported anywhere.
_RUNTIME = tempfile.mkdtemp(prefix="soc_test_")
os.environ.setdefault("SOC_DATA_DIR", str(Path(_RUNTIME) / "data"))
os.environ.setdefault("SOC_EVIDENCE_DIR", str(Path(_RUNTIME) / "evidence"))

from app.params import load_params  # noqa: E402


@pytest.fixture(scope="session")
def params():
    return load_params()
