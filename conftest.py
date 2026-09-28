"""Project-root conftest: makes the ``app`` package importable when pytest is
invoked from any working directory."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
