"""Pytest configuration and small project-bundle builders.

The builders compose genuine v3 lockfiles: tarballs are real gzip archives and
integrity strings are real sha512 digests of their bytes. Mutating a single
byte of a vendored tarball therefore produces a genuine cryptographic failure
— nothing about these tests is mocked.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests._fixtures import (  # noqa: E402
    make_bundle,
    make_tarball,
    manifest,
    npm_integrity,
    registry_url,
    vendor_path,
)

REGISTRY = "registry.example.com"


def lockfile3(
    packages: dict[str, dict[str, Any]],
    *,
    root_extra: dict[str, Any] | None = None,
    name: str = "offline-app",
    version: str = "1.0.0",
    lockfile_version: int = 3,
) -> bytes:
    root = {"name": name, "version": version}
    if root_extra:
        root.update(root_extra)
    packages = {"": root, **packages}
    doc = {
        "name": name,
        "version": version,
        "lockfileVersion": lockfile_version,
        "requires": True,
        "packages": packages,
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def node(
    name: str,
    version: str,
    *,
    tarball: bytes | None = None,
    deps: dict[str, str] | None = None,
    optional_deps: dict[str, str] | None = None,
    peer_deps: dict[str, str] | None = None,
    peer_optional: set[str] | None = None,
    dev_deps: dict[str, str] | None = None,
    os_constraints: list[str] | None = None,
    cpu_constraints: list[str] | None = None,
    libc_constraints: list[str] | None = None,
    integrity: str | bool | None = True,
    resolved: str | bool | None = True,
    optional: bool = False,
    in_bundle: bool = False,
    has_install_script: bool = False,
    registry: str = REGISTRY,
    version_override: str | None = None,
) -> tuple[str, dict[str, Any], tuple[str, bytes] | None]:
    """Return (lock_key, node_dict, vendor_tuple_or_None)."""
    key = f"node_modules/{name}"
    data: dict[str, Any] = {"version": version_override or version}
    url = registry_url(registry, name, version)
    if resolved is True:
        data["resolved"] = url
    elif isinstance(resolved, str):
        data["resolved"] = resolved
    if integrity is True and tarball is not None:
        data["integrity"] = npm_integrity(tarball)
    elif isinstance(integrity, str):
        data["integrity"] = integrity
    if deps:
        data["dependencies"] = dict(sorted(deps.items()))
    if optional_deps:
        data["optionalDependencies"] = dict(sorted(optional_deps.items()))
    if peer_deps:
        data["peerDependencies"] = dict(sorted(peer_deps.items()))
        if peer_optional:
            data["peerDependenciesMeta"] = {
                n: {"optional": True} for n in sorted(peer_optional)
            }
    if dev_deps:
        data["devDependencies"] = dict(sorted(dev_deps.items()))
    if os_constraints is not None:
        data["os"] = os_constraints
    if cpu_constraints is not None:
        data["cpu"] = cpu_constraints
    if libc_constraints is not None:
        data["libc"] = libc_constraints
    if optional:
        data["optional"] = True
    if in_bundle:
        data["inBundle"] = True
    if has_install_script:
        data["hasInstallScript"] = True
    vendored = None
    if tarball is not None:
        vendored = (vendor_path(registry, name, version), tarball)
    return key, data, vendored


def build_files(
    package_doc: bytes,
    lock_doc: bytes,
    vendored: list[tuple[str, bytes] | None],
    *,
    extra: dict[str, bytes] | None = None,
) -> dict[str, bytes]:
    files = {
        "package.json": package_doc,
        "package-lock.json": lock_doc,
    }
    for entry in vendored:
        if entry is not None:
            files[entry[0]] = entry[1]
    if extra:
        files.update(extra)
    return files


@pytest.fixture
def builders():
    return {
        "make_tarball": make_tarball,
        "make_bundle": make_bundle,
        "manifest": manifest,
        "lockfile3": lockfile3,
        "node": node,
        "build_files": build_files,
        "npm_integrity": npm_integrity,
        "registry": REGISTRY,
    }
