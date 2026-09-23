import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from maskcompiler.keys import load_keyfile, save_keyfile  # noqa: E402


@pytest.fixture()
def key_file(tmp_path):
    path = str(tmp_path / "local-test-keys.json")
    save_keyfile(path)
    return path


@pytest.fixture()
def bundle(key_file):
    return load_keyfile(key_file)
