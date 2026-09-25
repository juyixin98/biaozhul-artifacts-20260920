"""Shared pytest fixtures: a synthetic registry on a temp directory."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
import pytest

from model_switch.loader import ArtifactLoader
from model_switch.manager import ModelManager
from model_switch.manifest import (
    canonical_body_bytes,
    write_artifact,
)
from model_switch.model import build_version_weights, expected_warmup_logits

VALID_VERSIONS = ["v1", "v2", "v3"]
BAD_WARMUP_VERSION = "v-bad-warmup"


@pytest.fixture
def registry_root(tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    root.mkdir()
    for i, name in enumerate(VALID_VERSIONS, start=1):
        write_artifact(root, name, build_version_weights(i))
    # Checksum-valid weights, lying golden warm-up -> fails only at warm-up.
    bad = BAD_WARMUP_VERSION
    write_artifact(root, bad, build_version_weights(2))
    manifest_path = root / bad / "manifest.json"
    body = json.loads(manifest_path.read_text("utf-8"))
    body["warmup"]["expected_logits"] = [
        v + 1.0 for v in body["warmup"]["expected_logits"]
    ]
    raw = canonical_body_bytes(body)
    manifest_path.write_bytes(raw)
    (root / bad / "manifest.json.sha256").write_text(
        hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii"
    )
    return root


@pytest.fixture
def loader(registry_root: Path) -> ArtifactLoader:
    return ArtifactLoader(registry_root)


@pytest.fixture
def manager(loader: ArtifactLoader) -> ModelManager:
    return ModelManager(loader, switch_timeout_s=30.0)


@pytest.fixture
def active_manager(manager: ModelManager) -> ModelManager:
    manager.switch_to("v1")
    return manager


def reference_logits(version_index: int, x: np.ndarray) -> np.ndarray:
    """Independent reference forward pass for a seeded version.

    Used to prove that a prediction served as ``vN`` really used vN's
    weights — a half-loaded or mixed model could not match this.
    """
    weights = build_version_weights(version_index)
    x = np.asarray(x, dtype=np.float32)
    h = np.tanh(x @ weights["W1"] + weights["b1"])
    return np.asarray(h @ weights["W2"] + weights["b2"], dtype=np.float32)


VERSION_INDEX = {"v1": 1, "v2": 2, "v3": 3}
