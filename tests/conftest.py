import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from modelswitch.artifacts import write_artifact

INPUT_DIM = 8
OUTPUT_DIM = 3


@pytest.fixture()
def artifact_factory(tmp_path):
    """Return a function that writes a synthetic artifact into tmp_path."""
    counter = {"n": 0}

    def make(version: str, seed: int, weight_scale: float = 1.0) -> Path:
        counter["n"] += 1
        return write_artifact(
            tmp_path / f"art-{counter['n']}-{version}",
            version=version,
            seed=seed,
            input_dim=INPUT_DIM,
            output_dim=OUTPUT_DIM,
            weight_scale=weight_scale,
        )

    return make


@pytest.fixture()
def v1_dir(artifact_factory):
    return artifact_factory("v1", seed=101)


@pytest.fixture()
def v2_dir(artifact_factory):
    return artifact_factory("v2", seed=202)
