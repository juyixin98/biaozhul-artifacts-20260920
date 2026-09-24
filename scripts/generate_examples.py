"""Generate example request payloads from the constructed scenes.

Writes one JSON file per scene into examples/ (idempotent, deterministic).
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.segmentation.synth import SCENES  # noqa: E402

OUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                       "examples")


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    for name, factory in SCENES.items():
        scene = factory()
        payload = {
            "points": [
                {"id": int(i), "x": float(p[0]), "y": float(p[1]), "z": float(p[2])}
                for i, p in enumerate(scene.points)
            ]
        }
        path = os.path.join(OUT_DIR, f"{name}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        truth_path = os.path.join(OUT_DIR, f"{name}.truth.json")
        with open(truth_path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "scene": scene.name,
                    "description": scene.description,
                    "params": scene.params,
                    "labels": scene.labels,
                },
                fh,
                indent=2,
            )
        print(f"wrote {path} ({len(scene.points)} points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
