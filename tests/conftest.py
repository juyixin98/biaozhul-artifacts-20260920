"""Shared fixtures: in-memory bundles and tarballs."""
from __future__ import annotations

import base64
import hashlib
import io
import tarfile
from typing import Optional

import pytest

from app.lockgate.archive import Archive, parse_archive


def _add(tf: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    tf.addfile(info, io.BytesIO(data))


def make_tarball(name: str, version: str, payload: bytes = b"exports.x=1;\n",
                 install: bool = False) -> bytes:
    """Produce a real gzip npm-style tarball in memory."""
    import json
    meta = {"name": name, "version": version}
    if install:
        meta["scripts"] = {"install": "node-gyp rebuild"}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        _add(tf, "package/package.json", (json.dumps(meta) + "\n").encode())
        _add(tf, "package/index.js", payload)
    return buf.getvalue()


def integrity_of(blob: bytes, alg: str = "sha512") -> str:
    return f"{alg}-{base64.b64encode(hashlib.new(alg, blob).digest()).decode()}"


def registry_node(name: str, version: str, blob: bytes, deps: Optional[dict] = None,
                  **extra) -> dict:
    entry = {
        "version": version,
        "resolved": f"https://registry.npmjs.org/{name}/-/{name.split('/')[-1]}-{version}.tgz",
        "integrity": integrity_of(blob),
    }
    if deps:
        entry["dependencies"] = deps
    entry.update(extra)
    return entry


def bundle(files: dict[str, bytes], *, fmt: str = "tar.gz") -> Archive:
    """Build and parse a bundle archive entirely in memory."""
    buf = io.BytesIO()
    if fmt == "zip":
        import zipfile
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in files.items():
                zf.writestr(name, data)
    else:
        mode = "w:gz" if fmt == "tar.gz" else "w"
        with tarfile.open(fileobj=buf, mode=mode) as tf:
            for name, data in files.items():
                _add(tf, name, data)
    return parse_archive(buf.getvalue())


def vendor_path(name: str, version: str) -> str:
    return f"vendor/{name.split('/')[-1]}-{version}.tgz"


@pytest.fixture
def make_bundle():
    return bundle


@pytest.fixture
def blobs():
    class B:
        left_pad = make_tarball("left-pad", "1.2.3")
        left_pad_new = make_tarball("left-pad", "1.2.3", payload=b"exports.x=2;\n")
        ms = make_tarball("ms", "2.1.3")
        ms3 = make_tarball("ms", "3.0.0")
        debug = make_tarball("debug", "4.3.4")
    return B
