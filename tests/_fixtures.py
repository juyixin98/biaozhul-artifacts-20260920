"""Shared helpers for tests and the example-bundle generator.

Everything here builds *real* artifacts:

* tarballs are genuine gzip-compressed tar streams containing
  ``package/package.json`` — exactly the layout npm publishes;
* integrity strings are computed with :func:`hashlib.sha512` over the actual
  bytes and encoded the npm way (``sha512-<base64 raw digest>``);
* project bundles are genuine gzip tar archives, so the production extraction
  path is exercised end to end.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
from typing import Any


def npm_integrity(blob: bytes, algorithm: str = "sha512") -> str:
    digest = hashlib.new(algorithm, blob).digest()
    return f"{algorithm}-{base64.b64encode(digest).decode('ascii')}"


def make_tarball(
    name: str,
    version: str,
    *,
    scripts: dict[str, str] | None = None,
    extra_files: dict[str, bytes] | None = None,
    os_constraints: list[str] | None = None,
    cpu_constraints: list[str] | None = None,
    libc_constraints: list[str] | None = None,
) -> bytes:
    """Return bytes of a publishable npm tarball for ``name@version``."""
    manifest: dict[str, Any] = {"name": name, "version": version}
    if scripts:
        manifest["scripts"] = scripts
    if os_constraints is not None:
        manifest["os"] = os_constraints
    if cpu_constraints is not None:
        manifest["cpu"] = cpu_constraints
    if libc_constraints is not None:
        manifest["libc"] = libc_constraints

    files: dict[str, bytes] = {
        "package/package.json": json.dumps(
            manifest, indent=2, sort_keys=True
        ).encode("utf-8"),
        "package/README.md": f"# {name}\n".encode("utf-8"),
    }
    for rel_path, data in extra_files or {}:
        files[f"package/{rel_path}"] = data

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def registry_url(registry: str, name: str, version: str) -> str:
    base = name.removeprefix("@") if name.startswith("@") else name
    return f"https://{registry}/{base}/-/{base.split('/')[-1]}-{version}.tgz"


def vendor_path(registry: str, name: str, version: str) -> str:
    base = name.removeprefix("@") if name.startswith("@") else name
    file_name = f"{base.split('/')[-1]}-{version}.tgz"
    return f"vendor/{registry}/{base}/-/{file_name}"


def make_bundle(files: dict[str, bytes], *, mode: str = "w:gz") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode=mode) as tf:
        for path, data in sorted(files.items()):
            info = tarfile.TarInfo(name=path)
            info.size = len(data)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def lock_node(
    *,
    tarball: bytes | None,
    name: str,
    version: str,
    registry: str = "registry.example.com",
    deps: dict[str, str] | None = None,
    optional_deps: dict[str, str] | None = None,
    peer_deps: dict[str, str] | None = None,
    dev_deps: dict[str, str] | None = None,
    os_constraints: list[str] | None = None,
    cpu_constraints: list[str] | None = None,
    libc_constraints: list[str] | None = None,
    include_integrity: bool = True,
    include_resolved: bool = True,
    optional: bool = False,
    has_install_script: bool = False,
) -> tuple[dict[str, Any], tuple[str, bytes] | None]:
    """Build one v3 ``packages`` node plus its vendored tarball path/bytes."""
    node: dict[str, Any] = {"version": version}
    url = registry_url(registry, name, version)
    if include_resolved:
        node["resolved"] = url
    if include_integrity and tarball is not None:
        node["integrity"] = npm_integrity(tarball)
    if deps:
        node["dependencies"] = dict(sorted(deps.items()))
    if optional_deps:
        node["optionalDependencies"] = dict(sorted(optional_deps.items()))
    if peer_deps:
        node["peerDependencies"] = dict(sorted(peer_deps.items()))
    if dev_deps:
        node["devDependencies"] = dict(sorted(dev_deps.items()))
    if os_constraints is not None:
        node["os"] = os_constraints
    if cpu_constraints is not None:
        node["cpu"] = cpu_constraints
    if libc_constraints is not None:
        node["libc"] = libc_constraints
    if optional:
        node["optional"] = True
    if has_install_script:
        node["hasInstallScript"] = True
    vendored = None
    if tarball is not None:
        vendored = (vendor_path(registry, name, version), tarball)
    return node, vendored


def lockfile(
    packages: dict[str, dict[str, Any]], *, lockfile_version: int = 3
) -> bytes:
    doc = {
        "name": "offline-app",
        "version": "1.0.0",
        "lockfileVersion": lockfile_version,
        "requires": True,
        "packages": packages,
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


def manifest(
    *,
    name: str = "offline-app",
    version: str = "1.0.0",
    deps: dict[str, str] | None = None,
    optional_deps: dict[str, str] | None = None,
    peer_deps: dict[str, str] | None = None,
    dev_deps: dict[str, str] | None = None,
    workspaces: list[str] | None = None,
    extra: dict[str, Any] | None = None,
) -> bytes:
    doc: dict[str, Any] = {"name": name, "version": version}
    if deps:
        doc["dependencies"] = dict(sorted(deps.items()))
    if optional_deps:
        doc["optionalDependencies"] = dict(sorted(optional_deps.items()))
    if peer_deps:
        doc["peerDependencies"] = dict(sorted(peer_deps.items()))
    if dev_deps:
        doc["devDependencies"] = dict(sorted(dev_deps.items()))
    if workspaces is not None:
        doc["workspaces"] = workspaces
    if extra:
        doc.update(extra)
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")
