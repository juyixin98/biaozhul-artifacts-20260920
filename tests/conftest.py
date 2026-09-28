import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))  # for `import service.app`

from mptrainer.config import TrainerConfig  # noqa: E402
from mptrainer.data import SyntheticBatchSource  # noqa: E402
from mptrainer.trainer import MixedPrecisionTrainer  # noqa: E402

# Input amplification that reliably overflows fp16 (max ~65504): activations
# and scaled gradients cross into inf while fp32 stays perfectly finite.
OVERFLOW_AMPLIFY = 1.0e4


@pytest.fixture
def base_config() -> TrainerConfig:
    return TrainerConfig(
        layer_sizes=[8, 16, 1],
        seed=1234,
        base_lr=0.01,
        lr_decay=0.05,
        momentum=0.9,
        accum_steps=1,
        low_dtype="float16",
        init_scale=1024.0,
        growth_interval=4,
    ).validate()


@pytest.fixture
def source(base_config) -> SyntheticBatchSource:
    return SyntheticBatchSource(
        base_config.layer_sizes[0], base_config.layer_sizes[-1]
    )


@pytest.fixture
def trainer(base_config, tmp_path) -> MixedPrecisionTrainer:
    return MixedPrecisionTrainer(
        base_config, run_id="test-run", log_dir=tmp_path / "logs"
    )
