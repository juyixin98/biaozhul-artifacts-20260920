"""Test package bootstrap: make the project root importable.

Allows running `python -m unittest discover -s tests` from the repo root (the
project root is on sys.path via '' there) and also `python tests/...` directly.
"""

import os
import sys

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
