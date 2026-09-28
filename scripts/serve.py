"""Run the FastAPI service with locally frozen model artifacts.

    python scripts/serve.py [--models-dir models]

Models present in the directory at startup are loaded once; float reference
state (``*.trained.json``), when present, enables /validate.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from engine.model import load_artifact, load_trained_model
from service.app import create_app
from service.config import Settings
from service.registry import ModelRegistry


def build_registry(models_dir: Path) -> ModelRegistry:
    registry = ModelRegistry()
    if not models_dir.exists():
        return registry
    for artifact_path in sorted(models_dir.glob("*.json")):
        if artifact_path.name.endswith(".trained.json"):
            continue
        artifact = load_artifact(artifact_path)
        trained_path = models_dir / f"{artifact.model_id}.trained.json"
        trained = load_trained_model(trained_path) if trained_path.exists() else None
        registry.register(artifact, trained)
    return registry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--models-dir", default="models")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    settings = Settings.from_env()
    registry = build_registry(Path(args.models_dir))
    app = create_app(registry=registry, settings=settings)
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
