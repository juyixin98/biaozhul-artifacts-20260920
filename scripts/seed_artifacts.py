"""Generate a local, fully synthetic model registry.

No models or datasets are downloaded: every weight tensor is produced by a
seeded NumPy generator keyed by the version number (see
:func:`model_switch.model.build_version_weights`), so the registry is bit-for-
bit reproducible.

Two kinds of artifacts are produced:

* ``v1``, ``v2``, ``v3`` — valid versions with distinct weights.
* ``v-bad-warmup`` — checksum-valid weights whose signed manifest carries a
  deliberately wrong golden warm-up value, so the mandatory warm-up stage
  rejects it (used by the warm-up-failure acceptance fixture).

Usage::

    python -m scripts.seed_artifacts --root ./artifacts
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow running both as ``python -m scripts.seed_artifacts`` and as a file.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from model_switch.manifest import (  # noqa: E402
    canonical_body_bytes,
    write_artifact,
)
from model_switch.model import build_version_weights  # noqa: E402
import hashlib  # noqa: E402

VALID_VERSIONS = ["v1", "v2", "v3"]
BAD_WARMUP_VERSION = "v-bad-warmup"
# v-bad-warmup reuses v2's (valid) weights but lies about the golden output.
BAD_WARMUP_WEIGHTS_SOURCE = 2


def _corrupt_warmup_manifest(version_dir: Path) -> None:
    """Rewrite the manifest with an impossible golden warm-up, then re-sign.

    The tensor files and their hashes remain untouched and valid; only the
    golden expectation is wrong, so failure occurs strictly in the warm-up
    stage (proving warm-up is an independent gate, not a re-run of hashing).
    """
    manifest_path = version_dir / "manifest.json"
    body = json.loads(manifest_path.read_text("utf-8"))
    body["warmup"]["expected_logits"] = [
        v + 1.0 for v in body["warmup"]["expected_logits"]
    ]
    raw = canonical_body_bytes(body)
    manifest_path.write_bytes(raw)
    (version_dir / "manifest.json.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )


def build_registry(root: Path) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    created: list[str] = []

    for i, name in enumerate(VALID_VERSIONS, start=1):
        target = root / name
        if target.exists():
            continue
        weights = build_version_weights(i)
        write_artifact(root, name, weights)
        created.append(name)

    bad_dir = root / BAD_WARMUP_VERSION
    if not bad_dir.exists():
        write_artifact(root, BAD_WARMUP_VERSION, build_version_weights(BAD_WARMUP_WEIGHTS_SOURCE))
        _corrupt_warmup_manifest(bad_dir)
        created.append(BAD_WARMUP_VERSION)

    return {"root": str(root.resolve()), "created": created, "present": sorted(p.name for p in root.iterdir())}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="artifacts", help="registry root directory")
    args = parser.parse_args()
    result = build_registry(Path(args.root))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    print(f"\nvalid versions : {VALID_VERSIONS}")
    print(f"warm-up-failure fixture: {BAD_WARMUP_VERSION}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
