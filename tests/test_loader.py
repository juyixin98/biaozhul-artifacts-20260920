"""Loader pipeline tests: checksum, structural validation, warm-up."""
import json

import numpy as np
import pytest

from modelswitch.artifacts import MANIFEST_NAME, WEIGHTS_NAME
from modelswitch.loader import (
    ChecksumError,
    LoadStage,
    ValidationError,
    WarmupError,
    load_candidate,
)


def test_healthy_artifact_loads(v1_dir):
    version = load_candidate(v1_dir)
    assert version.version == "v1"
    assert version.input_dim == 8
    assert version.output_dim == 3
    out = version.predict(np.zeros((2, 8)))
    assert out.shape == (2, 3)
    assert np.allclose(out.sum(axis=1), 1.0)


def test_stages_run_in_order(v1_dir):
    seen = []
    load_candidate(v1_dir, on_stage=seen.append)
    assert seen == [
        LoadStage.READ_MANIFEST,
        LoadStage.VERIFY_CHECKSUM,
        LoadStage.LOAD_WEIGHTS,
        LoadStage.VALIDATE,
        LoadStage.BUILD_MODEL,
        LoadStage.WARMUP,
        LoadStage.READY,
    ]


def test_tampered_weights_fail_checksum(v1_dir):
    weights_path = v1_dir / WEIGHTS_NAME
    weights_path.write_bytes(weights_path.read_bytes() + b"tampered")
    with pytest.raises(ChecksumError):
        load_candidate(v1_dir)


def test_numerically_unusable_weights_fail_warmup(artifact_factory):
    # 1e308-scaled weights are structurally valid float64 but overflow to
    # non-finite logits at inference time: validation passes, warm-up fails.
    bad_dir = artifact_factory("v-bad", seed=303, weight_scale=1e308)
    with pytest.raises(WarmupError, match="non-finite"):
        load_candidate(bad_dir)


def test_missing_manifest_fails_validation(tmp_path):
    with pytest.raises(ValidationError, match="missing manifest"):
        load_candidate(tmp_path)


def test_shape_mismatch_fails_validation(v1_dir):
    manifest_path = v1_dir / MANIFEST_NAME
    manifest = json.loads(manifest_path.read_text())
    manifest["output_dim"] = 5  # weights are still (8, 3)
    manifest_path.write_text(json.dumps(manifest))
    with pytest.raises(ValidationError, match="does not match manifest"):
        load_candidate(v1_dir)


def test_predict_after_close_raises(v1_dir):
    version = load_candidate(v1_dir)
    version.close()
    assert version.closed
    with pytest.raises(RuntimeError, match="released"):
        version.predict(np.zeros(8))
