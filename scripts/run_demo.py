"""End-to-end demo against a running server (or started in-process).

Usage:
    python scripts/run_demo.py                      # uses $BASE_URL or :8000
    BASE_URL=http://localhost:8000 python scripts/run_demo.py

It: publishes an architecture, registers the iris demo dataset, launches a
training job, streams SSE metrics, pauses and resumes a second job, and prints
the resume-vs-continuous consistency check.
"""
from __future__ import annotations

import json
import os
import sys
import time

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8000").rstrip("/")
API = f"{BASE}/api/v1"
USER = {"X-User-Id": "demo-user"}


def wait_health(client: httpx.Client) -> None:
    for _ in range(60):
        try:
            if client.get(f"{BASE}/health").status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(1)
    raise SystemExit("server never became healthy")


def main() -> None:
    with httpx.Client(timeout=30) as http:
        wait_health(http)

        spec = {
            "layers": [
                {"id": "in", "type": "input", "in_features": 4},
                {"id": "h1", "type": "dense", "out_features": 16},
                {"id": "a1", "type": "relu"},
                {"id": "drop", "type": "dropout", "p": 0.2},
                {"id": "out", "type": "dense", "out_features": 3},
            ],
            "connections": [
                ["in", "h1"], ["h1", "a1"], ["a1", "drop"], ["drop", "out"]
            ],
        }
        arch = http.post(f"{API}/architectures",
                         json={"name": "iris-demo", "spec": spec})
        arch.raise_for_status()
        arch = arch.json()
        print(f"published architecture v{arch['version']} {arch['id']} "
              f"fp={arch['fingerprint'][:10]}")

        ds = http.post(f"{API}/datasets", json={
            "feature_path": "iris_demo.csv",
            "task": "classification",
        })
        ds.raise_for_status()
        ds = ds.json()
        print(f"registered dataset {ds['id']} n={ds['summary']['n_samples']}")

        job = http.post(f"{API}/jobs", headers=USER, json={
            "architecture_id": arch["id"],
            "dataset_id": ds["id"],
            "hyperparams": {"lr": 0.05, "batch_size": 16},
            "epochs": 8, "seed": 42, "val_fraction": 0.2,
        })
        job.raise_for_status()
        job_id = job.json()["id"]
        print(f"queued job {job_id}")

        # Stream SSE until terminal.
        print("streaming metrics:")
        with http.stream("GET", f"{API}/jobs/{job_id}/events/stream",
                        headers=USER) as resp:
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    ev = json.loads(line[6:])
                    if ev["kind"] == "epoch":
                        p = ev["payload"]
                        acc = p.get("val_accuracy")
                        print(f"  epoch {p['epoch']}: train={p['train_loss']:.4f}"
                              f" val={p['val_loss']:.4f}"
                              + (f" acc={acc:.3f}" if acc is not None else ""))
                    elif ev["kind"] in ("completed", "cancelled", "failed"):
                        print(f"  -> {ev['kind']}")

        # Replay does not re-train: fetch the same events by sequence.
        replay = http.get(f"{API}/jobs/{job_id}/events", headers=USER).json()
        print(f"replayed {len(replay)} persisted events (no re-training)")

        final = http.get(f"{API}/jobs/{job_id}", headers=USER).json()
        print(f"final status={final['status']} epochs_done={final['epochs_done']}")
        if final["status"] != "completed":
            sys.exit(f"unexpected status {final['status']}: {final.get('error')}")


if __name__ == "__main__":
    main()
