"""Generate the example bundles under ``examples/``.

Run::

    python scripts/build_examples.py

Every tarball is a real gzip archive and every ``integrity`` string in the
generated locks is computed over its actual bytes with sha512, so the
examples survive genuine cryptographic verification. One bundle is built
with a deliberately corrupted tarball to demonstrate failure reporting.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests._fixtures import (  # noqa: E402
    make_bundle,
    make_tarball,
    manifest,
    npm_integrity,
    registry_url,
    vendor_path,
)

REGISTRY = "registry.npmjs.org"
EXAMPLES = ROOT / "examples"


def _node(
    packages: dict,
    vendored: list,
    name: str,
    version: str,
    tarball: bytes,
    **fields,
) -> str:
    key = f"node_modules/{name}"
    data = {
        "version": version,
        "resolved": registry_url(REGISTRY, name, version),
        "integrity": npm_integrity(tarball),
    }
    data.update(fields)
    packages[key] = data
    vendored.append((vendor_path(REGISTRY, name, version), tarball))
    return key


def happy() -> tuple[str, bytes]:
    """A clean, fully vendored, offline-reproducible project."""
    packages: dict[str, object] = {
        "": {
            "name": "offline-console",
            "version": "3.1.4",
            "dependencies": {"chalk": "^5.3.0", "ms": "^2.1.0"},
        }
    }
    vendored: list[tuple[str, bytes]] = []

    chalk = make_tarball("chalk", "5.3.1")
    ms = make_tarball("ms", "2.1.3")
    _node(packages, vendored, "chalk", "5.3.1", chalk)
    _node(packages, vendored, "ms", "2.1.3", ms)

    pkg = manifest(
        name="offline-console",
        version="3.1.4",
        deps={"chalk": "^5.3.0", "ms": "^2.1.0"},
    )
    lock = _lock(packages, name="offline-console", version="3.1.4")
    files = {"package.json": pkg, "package-lock.json": lock}
    files.update(vendored)
    return "01-happy-reproducible.tgz", make_bundle(files)


def platform_optional() -> tuple[str, bytes]:
    """Optional native helper pinned for darwin/arm64 only."""
    packages: dict[str, object] = {
        "": {
            "name": "multi-platform-app",
            "version": "1.0.0",
            "optionalDependencies": {"native-helper": "^1.0.0"},
        }
    }
    vendored: list[tuple[str, bytes]] = []
    helper = make_tarball("native-helper", "1.0.0")
    _node(
        packages,
        vendored,
        "native-helper",
        "1.0.0",
        helper,
        os=["darwin"],
        cpu=["arm64"],
        optional=True,
    )
    pkg = manifest(
        name="multi-platform-app",
        version="1.0.0",
        optional_deps={"native-helper": "^1.0.0"},
    )
    lock = _lock(packages, name="multi-platform-app", version="1.0.0")
    files = {"package.json": pkg, "package-lock.json": lock}
    files.update(vendored)
    return "02-platform-optional.tgz", make_bundle(files)


def drift() -> tuple[str, bytes]:
    """Manifest asks ^1.x but the lock resolved 2.0.0: classic lock drift."""
    packages: dict[str, object] = {
        "": {
            "name": "drifty",
            "version": "1.0.0",
            "dependencies": {"left-pad": "^1.3.0"},
        }
    }
    vendored: list[tuple[str, bytes]] = []
    tar = make_tarball("left-pad", "2.0.0")
    _node(packages, vendored, "left-pad", "2.0.0", tar)
    pkg = manifest(name="drifty", version="1.0.0", deps={"left-pad": "^1.3.0"})
    lock = _lock(packages, name="drifty", version="1.0.0")
    files = {"package.json": pkg, "package-lock.json": lock}
    files.update(vendored)
    return "03-lock-drift.tgz", make_bundle(files)


def missing_integrity() -> tuple[str, bytes]:
    """A registry node with no integrity field."""
    tar = make_tarball("unpinned", "4.2.0")
    packages: dict[str, object] = {
        "": {
            "name": "loose-app",
            "version": "1.0.0",
            "dependencies": {"unpinned": "^4.2.0"},
        },
        "node_modules/unpinned": {
            "version": "4.2.0",
            "resolved": registry_url(REGISTRY, "unpinned", "4.2.0"),
        },
    }
    pkg = manifest(name="loose-app", version="1.0.0", deps={"unpinned": "^4.2.0"})
    lock = _lock(packages, name="loose-app", version="1.0.0")
    files = {
        "package.json": pkg,
        "package-lock.json": lock,
        vendor_path(REGISTRY, "unpinned", "4.2.0"): tar,
    }
    return "04-missing-integrity.tgz", make_bundle(files)


def cycle() -> tuple[str, bytes]:
    """Two packages that depend on each other."""
    packages: dict[str, object] = {
        "": {
            "name": "loopy",
            "version": "1.0.0",
            "dependencies": {"alpha": "^1.0.0"},
        }
    }
    vendored: list[tuple[str, bytes]] = []
    alpha = make_tarball("alpha", "1.0.0")
    beta = make_tarball("beta", "1.0.0")
    _node(packages, vendored, "alpha", "1.0.0", alpha, dependencies={"beta": "^1.0.0"})
    _node(packages, vendored, "beta", "1.0.0", beta, dependencies={"alpha": "^1.0.0"})
    pkg = manifest(name="loopy", version="1.0.0", deps={"alpha": "^1.0.0"})
    lock = _lock(packages, name="loopy", version="1.0.0")
    files = {"package.json": pkg, "package-lock.json": lock}
    files.update(vendored)
    return "05-circular-dependency.tgz", make_bundle(files)


def tampered() -> tuple[str, bytes]:
    """The lock is internally consistent but the vendored bytes were swapped."""
    packages: dict[str, object] = {
        "": {
            "name": "supply-chain-demo",
            "version": "1.0.0",
            "dependencies": {"left-pad": "^1.3.0"},
        }
    }
    vendored: list[tuple[str, bytes]] = []
    original = make_tarball("left-pad", "1.3.0")
    path = vendor_path(REGISTRY, "left-pad", "1.3.0")
    _node(packages, vendored, "left-pad", "1.3.0", original)
    # Replace the artifact with different bytes but leave the lock's hash.
    swapped = make_tarball(
        "left-pad", "1.3.0", extra_files=[("TAMPERED.txt", b"not the published bytes")]
    )
    pkg = manifest(
        name="supply-chain-demo", version="1.0.0", deps={"left-pad": "^1.3.0"}
    )
    lock = _lock(packages, name="supply-chain-demo", version="1.0.0")
    files = {"package.json": pkg, "package-lock.json": lock, path: swapped}
    return "06-tampered-tarball.tgz", make_bundle(files)


def duplicates() -> tuple[str, bytes]:
    """util-x@1.0.0 nested under two parents; identical content -> warning,
    plus a second distinct version (1.0.0 vs 2.0.0) -> info."""
    packages: dict[str, object] = {
        "": {
            "name": "dup-app",
            "version": "1.0.0",
            "dependencies": {"host-a": "^1.0.0", "host-b": "^1.0.0", "util-x": "^1.0.0"},
        }
    }
    vendored: list[tuple[str, bytes]] = []
    util1 = make_tarball("util-x", "1.0.0")
    ha = make_tarball("host-a", "1.0.0")
    hb = make_tarball("host-b", "1.0.0")
    _node(packages, vendored, "host-a", "1.0.0", ha, dependencies={"util-x": "^1.0.0"})
    _node(packages, vendored, "host-b", "1.0.0", hb, dependencies={"util-x": "^1.0.0"})
    _node(packages, vendored, "util-x", "1.0.0", util1)
    # A nested duplicate copy with identical bytes/integrity.
    nested_key = "node_modules/host-a/node_modules/util-x"
    packages[nested_key] = {
        "version": "1.0.0",
        "resolved": registry_url(REGISTRY, "util-x", "1.0.0"),
        "integrity": npm_integrity(util1),
    }
    vendored.append((vendor_path(REGISTRY, "util-x", "1.0.0"), util1))
    pkg = manifest(
        name="dup-app",
        version="1.0.0",
        deps={"host-a": "^1.0.0", "host-b": "^1.0.0", "util-x": "^1.0.0"},
    )
    lock = _lock(packages, name="dup-app", version="1.0.0")
    files = {"package.json": pkg, "package-lock.json": lock}
    files.update(vendored)
    return "08-duplicate-versions.tgz", make_bundle(files)


def unsupported() -> tuple[str, bytes]:
    """Manifest references a github dependency: refused, not resolved."""
    packages: dict[str, object] = {
        "": {
            "name": "wild-app",
            "version": "1.0.0",
            "dependencies": {"evil": "github:evil/owner#deadbeef"},
        }
    }
    pkg = manifest(
        name="wild-app",
        version="1.0.0",
        deps={"evil": "github:evil/owner#deadbeef"},
    )
    lock = _lock(packages, name="wild-app", version="1.0.0")
    return "07-unsupported-syntax.tgz", make_bundle(
        {"package.json": pkg, "package-lock.json": lock}
    )


def _lock(packages: dict, *, name: str, version: str) -> bytes:
    doc = {
        "name": name,
        "version": version,
        "lockfileVersion": 3,
        "requires": True,
        "packages": packages,
    }
    return (json.dumps(doc, indent=2) + "\n").encode("utf-8")


BUILDERS = [
    happy,
    platform_optional,
    drift,
    missing_integrity,
    cycle,
    duplicates,
    tampered,
    unsupported,
]


def main() -> None:
    EXAMPLES.mkdir(exist_ok=True)
    for builder in BUILDERS:
        name, blob = builder()
        target = EXAMPLES / name
        target.write_bytes(blob)
        print(f"wrote {target.relative_to(ROOT)} ({len(blob)} bytes)")


if __name__ == "__main__":
    main()
