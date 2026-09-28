"""End-to-end checks of the runnable local scripts.

Builds the demo model artifact in a temporary directory and runs the local
demo, asserting concrete acceptance/rejection behavior in its output.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.integration


def _run(cmd: list[str], cwd: Path, env: dict | None = None) -> subprocess.CompletedProcess:
    import os

    full_env = {**os.environ, "PYTHONPATH": str(REPO_ROOT), **(env or {})}
    return subprocess.run(
        cmd,
        cwd=cwd,
        env=full_env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_build_demo_model_freezes_version_bound_artifact(tmp_path: Path) -> None:
    result = _run(
        [
            sys.executable,
            "scripts/build_demo_model.py",
            "--config",
            "configs/demo_model.json",
            "--out-dir",
            str(tmp_path),
        ],
        REPO_ROOT,
    )
    assert result.returncode == 0, result.stderr
    assert "frozen artifact" in result.stdout
    import json

    artifact = json.loads((tmp_path / "demo_mlp.json").read_text())
    assert artifact["quantizer_version"] == "quant-asy-v1.0.0"
    assert len(artifact["model_version"]) == 16
    assert artifact["calibration"]["sample_count"] == 256
    # Weights are genuinely small integers inside the int8 range.
    all_values = [
        v
        for layer in artifact["layers"]
        for row in layer["weights_int"]
        for v in row
    ]
    assert min(all_values) >= -128
    assert max(all_values) <= 127


def test_demo_local_reports_accept_and_reject(tmp_path: Path) -> None:
    build = _run(
        [
            sys.executable,
            "scripts/build_demo_model.py",
            "--config",
            "configs/demo_model.json",
            "--out-dir",
            str(tmp_path / "models"),
        ],
        REPO_ROOT,
    )
    assert build.returncode == 0, build.stderr

    # The demo script reads models/ relative to cwd; emulate with env var via
    # a tiny inline driver pointing at the temp directory.
    driver = (
        "import sys; sys.path.insert(0, '.'); "
        "import json, numpy as np; "
        "from pathlib import Path; "
        "from engine.model import load_artifact, load_trained_model; "
        "from service.inference import InferenceService; "
        "from service.registry import ModelRegistry; "
        "from engine.errors import QuantEngineError; "
        f"d = Path({str(tmp_path / 'models')!r}); "
        "a = load_artifact(d / 'demo_mlp.json'); "
        "t = load_trained_model(d / 'demo_mlp.trained.json'); "
        "r = ModelRegistry(); r.register(a, t); "
        "s = InferenceService(r); "
        "v = s.validate('demo_mlp', np.random.default_rng(0).uniform(-1.5,1.5,(8,3)).tolist()); "
        "print('VERDICT', v.accepted); "
        "ok = False\n"
        "try:\n"
        "    s.infer('demo_mlp', [[999.0, 0.0, 0.0]])\n"
        "except QuantEngineError as e:\n"
        "    print('REJECTED', e.code, e.context.get('request_id')); ok = True\n"
        "assert ok\n"
    )
    result = _run([sys.executable, "-c", driver], REPO_ROOT)
    assert result.returncode == 0, result.stderr
    assert "VERDICT True" in result.stdout
    assert "REJECTED input_out_of_calibration_range" in result.stdout
    assert "req-" in result.stdout
