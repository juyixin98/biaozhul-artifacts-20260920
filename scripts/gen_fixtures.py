#!/usr/bin/env python3
"""Generate local fixtures for the manifest-selector demo.

No network access is performed. Every blob is serialized canonically
(``json.dumps(..., sort_keys=True, separators=(",", ":"))`` — the same byte
order serde_json produces by default) and addressed by its real sha256, so
the output contains no hand-written digests.
"""

import hashlib
import json
import os
import sys

OCI_INDEX = "application/vnd.oci.image.index.v1+json"
OCI_MANIFEST = "application/vnd.oci.image.manifest.v1+json"

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "..", "fixtures")


def canon(obj) -> bytes:
    """Canonical JSON bytes: sorted keys, no whitespace (matches serde_json)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()


def digest_of(obj_or_bytes) -> str:
    if isinstance(obj_or_bytes, bytes):
        data = obj_or_bytes
    else:
        data = canon(obj_or_bytes)
    return "sha256:" + hashlib.sha256(data).hexdigest()


def manifest(name: str) -> dict:
    """A distinct leaf image manifest for `name`."""
    return {
        "schemaVersion": 2,
        "mediaType": OCI_MANIFEST,
        "config": {
            "mediaType": "application/vnd.oci.image.config.v1+json",
            "digest": "sha256:" + hashlib.sha256(f"config:{name}".encode()).hexdigest(),
            "size": 100,
        },
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar+gzip",
                "digest": "sha256:" + hashlib.sha256(f"layer:{name}".encode()).hexdigest(),
                "size": 1024,
            }
        ],
    }


def child_descriptor(leaf: dict, platform: dict, size: int | None = None) -> dict:
    data = canon(leaf)
    d = {
        "mediaType": OCI_MANIFEST,
        "digest": digest_of(data),
        "size": size if size is not None else len(data),
        "platform": platform,
    }
    return d


def index(children: list[dict]) -> dict:
    return {
        "schemaVersion": 2,
        "mediaType": OCI_INDEX,
        "manifests": children,
    }


def write_fixture(repo: str, blobs: list, tags: dict) -> None:
    out_dir = os.path.join(FIXTURES, repo)
    os.makedirs(out_dir, exist_ok=True)
    doc = {"blobs": [{"json": b} for b in blobs], "tags": tags}
    with open(os.path.join(out_dir, "fixture.json"), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=2, sort_keys=True)
        fh.write("\n")
    print(f"wrote {repo}: {len(blobs)} blobs, tags {list(tags)}")


def multiarch() -> None:
    """demo/multiarch — ARM variants, osVersion, osFeatures, missing platform."""
    linux_amd64 = manifest("linux-amd64")
    linux_arm_v5 = manifest("linux-arm-v5")
    linux_arm_v6 = manifest("linux-arm-v6")
    linux_arm_v7 = manifest("linux-linuxarm-v7")
    arm64_v8 = manifest("linux-arm64-v8")
    win_1809 = manifest("win-amd64-1809")
    win_ltsc = manifest("win-amd64-ltsc2022")
    # Slightly unusual: the array is deliberately ordered so the "first"
    # entry is never the right answer for the ARM scenarios.
    children = [
        child_descriptor(win_ltsc, {
            "architecture": "amd64", "os": "windows",
            "osVersion": "10.0.20348",
        }),
        child_descriptor(linux_arm_v5, {
            "architecture": "arm", "os": "linux", "variant": "v5",
        }),
        child_descriptor(linux_amd64, {
            "architecture": "amd64", "os": "linux",
        }),
        child_descriptor(linux_arm_v7, {
            "architecture": "arm", "os": "linux", "variant": "v7",
        }),
        child_descriptor(win_1809, {
            "architecture": "amd64", "os": "windows",
            "osVersion": "10.0.17763.5235",
            "osFeatures": ["win32k"],
        }),
        child_descriptor(arm64_v8, {
            "architecture": "arm64", "os": "linux", "variant": "v8",
        }),
        child_descriptor(linux_arm_v6, {
            "architecture": "arm", "os": "linux", "variant": "v6",
        }),
    ]
    root = index(children)
    blobs = [
        root,
        linux_amd64, linux_arm_v5, linux_arm_v6, linux_arm_v7, arm64_v8,
        win_1809, win_ltsc,
    ]
    write_fixture("demo-multiarch", blobs, {"latest": digest_of(root)})


def ambiguous() -> None:
    """demo/ambiguous — two distinct blobs with identical platform tuples."""
    leaf_a = manifest("amb-a")
    leaf_b = manifest("amb-b")
    plat = {"architecture": "arm64", "os": "linux", "variant": "v8"}
    root = index([
        child_descriptor(leaf_a, plat),
        child_descriptor(leaf_b, plat),
    ])
    write_fixture("demo-ambiguous", [root, leaf_a, leaf_b],
                  {"latest": digest_of(root)})


def cycle_case() -> None:
    """No on-disk fixture for a manifest cycle.

    A reachable digest-consistent cycle cannot be authored without a hash
    collision: the closing edge either names a missing blob or fails digest
    verification, so the selector always reports ``missing_blob`` /
    ``digest_mismatch`` first. The traversal cycle guard is nevertheless
    implemented as defense in depth and is exercised directly in
    ``tests/select_test.rs``; the tampered-ring HTTP behavior is demonstrated
    in ``tests/api_test.rs`` via the test-only claim endpoint.
    """
    return


def nested() -> None:
    """demo/nested — index of indexes; selection walks two levels."""
    leaf_v7 = manifest("nested-arm-v7")
    leaf_amd64 = manifest("nested-amd64")
    inner_arm = index([
        child_descriptor(leaf_v7, {"architecture": "arm", "os": "linux", "variant": "v7"}),
    ])
    inner_amd64 = index([
        child_descriptor(leaf_amd64, {"architecture": "amd64", "os": "linux"}),
    ])
    root = index([
        {
            "mediaType": OCI_INDEX,
            "digest": digest_of(inner_arm),
            "size": len(canon(inner_arm)),
            "platform": {"architecture": "arm", "os": "linux"},
        },
        {
            "mediaType": OCI_INDEX,
            "digest": digest_of(inner_amd64),
            "size": len(canon(inner_amd64)),
            "platform": {"architecture": "amd64", "os": "linux"},
        },
    ])
    write_fixture(
        "demo-nested",
        [root, inner_arm, inner_amd64, leaf_v7, leaf_amd64],
        {"latest": digest_of(root)},
    )


def missing_blob() -> None:
    """demo/missing — root descriptor points at a digest with no stored blob."""
    leaf = manifest("ghost")
    ghost_digest = "sha256:" + "ab" * 32
    root = index([
        {
            "mediaType": OCI_MANIFEST,
            "digest": ghost_digest,
            "size": 123,
            "platform": {"architecture": "amd64", "os": "linux"},
        },
        child_descriptor(leaf, {"architecture": "arm64", "os": "linux"}),
    ])
    write_fixture("demo-missing", [root, leaf], {"latest": digest_of(root)})


def main() -> None:
    os.makedirs(FIXTURES, exist_ok=True)
    multiarch()
    ambiguous()
    cycle_case()
    nested()
    missing_blob()
    print("all fixtures generated under", os.path.normpath(FIXTURES))


if __name__ == "__main__":
    main()
