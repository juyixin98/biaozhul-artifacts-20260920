# Ensures the project root is importable when pytest is run from any directory.
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
