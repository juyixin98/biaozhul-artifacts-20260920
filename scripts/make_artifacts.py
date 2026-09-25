#!/usr/bin/env python3
"""Generate reproducible synthetic model artifacts for demos and tests.

Usage:
    python scripts/make_artifacts.py [--output artifacts]

Creates:
    artifacts/v1, artifacts/v2        healthy artifacts (different seeds)
    artifacts/v3-bad-warmup           passes checksum/validation, fails warm-up
    artifacts/v4-bad-checksum         manifest sha256 does not match weights
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modelswitch.artifacts import WEIGHTS_NAME, write_artifact


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts")
    args = parser.parse_args()
    out = Path(args.output)

    write_artifact(out / "v1", version="v1", seed=101)
    write_artifact(out / "v2", version="v2", seed=202)

    # Numerically unusable weights: structurally valid, warm-up must reject.
    write_artifact(out / "v3-bad-warmup", version="v3-bad-warmup", seed=303,
                   weight_scale=1e308)

    # Tampered weights: regenerate manifest-mismatching bytes after writing.
    bad = write_artifact(out / "v4-bad-checksum", version="v4-bad-checksum", seed=404)
    weights_path = bad / WEIGHTS_NAME
    weights_path.write_bytes(weights_path.read_bytes() + b"tampered")

    for path in sorted(out.iterdir()):
        print(f"wrote {path}")


if __name__ == "__main__":
    main()
