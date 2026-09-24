"""Workspace modelling tests (glob expansion, links, drift)."""

from __future__ import annotations

import json

from tests._fixtures import make_tarball, manifest
from app.verifier import verify_bundle


def test_workspace_link_roundtrip(builders):
    b = builders
    lib_tar = None
    # Workspace package "tools" at packages/tools, linked from root node_modules.
    ws_manifest = manifest(name="tools", version="0.4.2")
    packages = {
        "": {
            "name": "offline-app",
            "version": "1.0.0",
            "dependencies": {"tools": "workspace:^"},
        },
        "packages/tools": {
            "name": "tools",
            "version": "0.4.2",
        },
        "node_modules/tools": {
            "resolved": "packages/tools",
            "link": True,
        },
    }
    lock = (
        json.dumps(
            {
                "name": "offline-app",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "requires": True,
                "packages": packages,
            },
            indent=2,
        )
        + "\n"
    ).encode()
    pkg = manifest(
        deps={"tools": "workspace:^"}, workspaces=["packages/*"]
    )
    files = {
        "package.json": pkg,
        "package-lock.json": lock,
        "packages/tools/package.json": ws_manifest,
    }
    report = verify_bundle(files)
    assert report["status"] == "pass", json.dumps(report, indent=2)
    assert report["summary"]["workspace_nodes"] == 1
    assert report["summary"]["link_nodes"] == 1


def test_workspace_version_drift(builders):
    packages = {
        "": {"name": "offline-app", "version": "1.0.0"},
        "packages/tools": {"name": "tools", "version": "9.9.9"},
    }
    lock = (
        json.dumps(
            {
                "name": "offline-app",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": packages,
            }
        )
    ).encode()
    files = {
        "package.json": manifest(workspaces=["packages/*"]),
        "package-lock.json": lock,
        "packages/tools/package.json": manifest(name="tools", version="0.1.0"),
    }
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "WORKSPACE_VERSION_DRIFT" in codes


def test_workspace_glob_expansion_and_unsupported_patterns(builders):
    files = {
        "package.json": manifest(workspaces=["packages/[abc]"]),
        "package-lock.json": (
            json.dumps(
                {
                    "name": "offline-app",
                    "version": "1.0.0",
                    "lockfileVersion": 3,
                    "packages": {"": {"name": "offline-app", "version": "1.0.0"}},
                }
            )
        ).encode(),
    }
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "WORKSPACE_PATTERN_UNSUPPORTED" in codes


def test_undeclared_workspace_in_lock_is_error(builders):
    packages = {
        "": {"name": "offline-app", "version": "1.0.0"},
        "rogue/place": {"name": "rogue", "version": "0.0.1"},
    }
    lock = (
        json.dumps(
            {
                "name": "offline-app",
                "version": "1.0.0",
                "lockfileVersion": 3,
                "packages": packages,
            }
        )
    ).encode()
    files = {
        "package.json": manifest(),
        "package-lock.json": lock,
        "rogue/place/package.json": manifest(name="rogue", version="0.0.1"),
    }
    report = verify_bundle(files)
    codes = {f["code"] for f in report["findings"]}
    assert "LOCK_WORKSPACE_UNDECLARED" in codes
