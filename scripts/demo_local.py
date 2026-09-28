"""Local demo: build-free in-process run of inference and validation.

Runs three labeled requests against the demo model:

1. an in-range request            -> expected ACCEPT;
2. an out-of-calibration request  -> expected REJECT (typed error, request id);
3. a batch request with metrics   -> error vs the float reference, bounded.

Run the model builder first::

    python scripts/build_demo_model.py
    python scripts/demo_local.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from engine.errors import QuantEngineError
from engine.model import load_artifact, load_trained_model
from service.inference import InferenceService
from service.registry import ModelRegistry


def main() -> None:
    models_dir = Path("models")
    model_id = "demo_mlp"
    artifact = load_artifact(models_dir / f"{model_id}.json")
    trained = load_trained_model(models_dir / f"{model_id}.trained.json")

    registry = ModelRegistry()
    registry.register(artifact, trained)
    service = InferenceService(
        registry, enforce_calibration_range=True
    )

    rng = np.random.default_rng(7)
    in_range = rng.uniform(-1.5, 1.5, size=(2, 3)).tolist()
    out_of_range = (rng.uniform(-1.5, 1.5, size=(1, 3)) + np.array([50.0, 0.0, 0.0])).tolist()

    print("== 1. in-range request ==")
    response = service.infer(model_id, in_range)
    print(json.dumps({
        "request_id": response.request_id,
        "output": response.output,
        "diagnostics": response.diagnostics,
    }, indent=2))

    print("\n== 2. out-of-calibration request (expect REJECT) ==")
    try:
        service.infer(model_id, out_of_range)
    except QuantEngineError as exc:
        print(json.dumps({
            "category": exc.category.value,
            "code": exc.code,
            "http_status": exc.http_status,
            "message": exc.message,
            "context": exc.context,
        }, indent=2, default=str))

    print("\n== 3. validation against the float reference ==")
    batch = rng.uniform(-1.8, 1.8, size=(16, 3)).tolist()
    verdict = service.validate(model_id, batch)
    print(json.dumps({
        "request_id": verdict.request_id,
        "decision": "ACCEPT" if verdict.accepted else "INDETERMINATE",
        "reason": verdict.reason,
        "error_report": verdict.error_report,
    }, indent=2))


if __name__ == "__main__":
    main()
