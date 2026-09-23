"""pytest 公共夹具：独立数据目录 + 由生成器构建的样例。"""

import json
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.api import create_app  # noqa: E402
from app.samples.generate import VARIANTS, build_malicious_packages, build_variant  # noqa: E402
from app.storage import Storage  # noqa: E402


@pytest.fixture()
def tmp_data(tmp_path):
    return tmp_path / "data"


@pytest.fixture()
def storage(tmp_data):
    st = Storage(str(tmp_data / "pv.db"), str(tmp_data / "jobs"))
    yield st
    st.close()


@pytest.fixture()
def client(storage):
    return TestClient(create_app(storage))


@pytest.fixture()
def examples(tmp_path):
    out = tmp_path / "examples"
    out.mkdir()
    for v in VARIANTS:
        build_variant(v, str(out))
    build_malicious_packages(str(out))
    return out


def load_variant(vdir: Path) -> dict:
    cfg = json.loads((vdir / "config.json").read_text())
    co = json.loads((vdir / "compilerOutput.json").read_text())
    target = (vdir / "target_deployed.hex").read_text().strip()
    with zipfile.ZipFile(vdir / "sources.zip") as zf:
        sources = {n: zf.read(n) for n in zf.namelist()}
    expected = json.loads((vdir / "README.json").read_text())["expected_verdict"]
    return {"config": cfg, "compilerOutput": co, "target": target, "sources": sources, "expected": expected}


def submit_dir(client, vdir: Path, package_name: str = "sources.zip"):
    with open(vdir / package_name, "rb") as fh:
        return client.post(
            "/api/v1/verify",
            files={"package": (package_name, fh, "application/octet-stream")},
            data={
                "config": (vdir / "config.json").read_text(),
                "compilerOutput": (vdir / "compilerOutput.json").read_text(),
                "targetDeployed": (vdir / "target_deployed.hex").read_text().strip(),
            },
        )
