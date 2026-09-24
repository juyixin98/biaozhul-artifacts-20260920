import os
import sys

# Make both the app package and the scripts/ directory importable in tests.
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import pytest  # noqa: E402


def pytest_addoption(parser):
    parser.addoption(
        "--skip-slow", action="store_true",
        help="skip the real-network end-to-end tests",
    )


def pytest_collection_modifyitems(config, items):
    if not config.getoption("--skip-slow"):
        return
    skip = pytest.mark.skip(reason="--skip-slow given")
    for item in items:
        if "e2e" in str(item.fspath):
            item.add_marker(skip)
