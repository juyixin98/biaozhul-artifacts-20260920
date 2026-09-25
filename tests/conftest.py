"""Pytest bootstrap: make src/ importable without an installed package."""

import os
import sys

SRC = os.path.join(os.path.dirname(__file__), "..", "src")
sys.path.insert(0, os.path.abspath(SRC))
