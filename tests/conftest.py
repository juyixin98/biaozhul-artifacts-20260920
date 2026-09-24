import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.params import Params, load_and_verify

REPO_ROOT = Path(__file__).resolve().parent.parent
PARAM_DIR = REPO_ROOT / "config"


@pytest.fixture(scope="session")
def params() -> Params:
    key = bytes.fromhex((PARAM_DIR / "param_key.dev.hex").read_text().strip())
    signed = load_and_verify(PARAM_DIR, key)
    return Params.from_manifest(signed.manifest)


@pytest.fixture()
def data_dir(tmp_path) -> Path:
    return tmp_path / "data"


@pytest.fixture()
def client(data_dir):
    app = create_app(data_dir=data_dir, param_dir=PARAM_DIR)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def tampered_param_dir(tmp_path) -> Path:
    """A copy of the frozen params with one value modified (unsigned change)."""
    dst = tmp_path / "config"
    shutil.copytree(PARAM_DIR, dst)
    manifest = json.loads((dst / "params.json").read_text())
    manifest["capacity"]["nominal_ah"] = 51.0  # tamper
    (dst / "params.json").write_text(json.dumps(manifest, indent=2))
    return dst
