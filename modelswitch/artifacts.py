"""Synthetic model-artifact generation.

An artifact is a directory:

    <dir>/
        manifest.json   {"version", "input_dim", "output_dim", "seed", "sha256"}
        weights.npz     arrays "W" (input_dim x output_dim) and "b" (output_dim,)

`sha256` in the manifest is the hex digest of the exact bytes of weights.npz,
so any tampering with the weights file is detected at load time.

Everything is generated from an explicit seed: no downloads, fully
reproducible.
"""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import numpy as np

MANIFEST_NAME = "manifest.json"
WEIGHTS_NAME = "weights.npz"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_weights(seed: int, input_dim: int, output_dim: int) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(seed)
    weights = rng.normal(loc=0.0, scale=1.0, size=(input_dim, output_dim))
    bias = rng.normal(loc=0.0, scale=0.1, size=(output_dim,))
    return weights, bias


def write_artifact(
    directory: str | Path,
    *,
    version: str,
    seed: int,
    input_dim: int = 8,
    output_dim: int = 3,
    weight_scale: float = 1.0,
) -> Path:
    """Write a reproducible synthetic artifact and return its directory.

    `weight_scale` multiplies the weights; an absurd value (e.g. 1e308) yields
    an artifact that passes structural validation but fails warm-up, which the
    test fixtures use.
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    weights, bias = build_weights(seed, input_dim, output_dim)
    with np.errstate(over="ignore", invalid="ignore"):
        weights = weights * weight_scale

    weights_path = directory / WEIGHTS_NAME
    # Serialize via an in-memory buffer so the on-disk bytes are exactly what
    # we hash below (np.savez_compressed output is deterministic for fixed
    # arrays on a given platform, but hashing the buffer removes all doubt).
    buffer = io.BytesIO()
    np.savez(buffer, W=weights, b=bias)
    payload = buffer.getvalue()
    weights_path.write_bytes(payload)

    manifest = {
        "version": version,
        "input_dim": input_dim,
        "output_dim": output_dim,
        "seed": seed,
        "sha256": hashlib.sha256(payload).hexdigest(),
    }
    (directory / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2) + "\n")
    return directory
