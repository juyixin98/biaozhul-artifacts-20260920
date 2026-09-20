#!/usr/bin/env python3
"""End-to-end demo of the SynapticGo API using only the standard library.

Usage:
    python3 examples/demo.py [base_url]

Requires a running server (e.g. `docker compose up`).
"""
import hashlib
import json
import os
import random
import sys
import urllib.request
import urllib.error

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080"
USER = "demo-user"


def call(method, path, body=None, raw=None, headers=None):
    req = urllib.request.Request(BASE + path, method=method)
    req.add_header("X-User-ID", USER)
    data = None
    if raw is not None:
        data = raw
    elif body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, data=data) as resp:
            payload = resp.read()
            return resp.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def sha256(b):
    return hashlib.sha256(b).hexdigest()


def main():
    random.seed(7)

    # 1. Create a dataset and upload it in chunks, out of order.
    content = bytes(random.randrange(256) for _ in range(5000))
    chunk_size = 2000
    status, ds = call("POST", "/datasets", {
        "name": "demo-dataset",
        "total_size": len(content),
        "chunk_size": chunk_size,
        "sha256": sha256(content),
    })
    assert status == 201, ds
    ds_id = ds["id"]
    print(f"dataset {ds_id} created, {ds['chunk_count']} chunks")

    chunks = [content[i:i + chunk_size] for i in range(0, len(content), chunk_size)]
    for idx in [2, 0, 1]:  # out of order on purpose
        status, chunk = call("PUT", f"/datasets/{ds_id}/chunks/{idx}",
                             raw=chunks[idx],
                             headers={"X-Chunk-SHA256": sha256(chunks[idx])})
        assert status == 201, chunk
        print(f"  chunk {idx} uploaded")

    # 2. Publish: merges chunks, verifies the overall digest.
    status, ds = call("POST", f"/datasets/{ds_id}/publish")
    assert status == 200, ds
    print(f"dataset published: {ds['status']}")

    # 3. Register a model and an immutable linear-classifier version.
    status, model = call("POST", "/models", {"name": "demo-model"})
    assert status == 201, model
    model_id = model["id"]

    status, version = call("POST", f"/models/{model_id}/versions", {
        "dataset_id": ds_id,
        "input_dim": 3,
        "labels": ["setosa", "versicolor"],
        "weights": [[1.0, 0.0, -1.0], [0.0, 1.0, 1.0]],
        "bias": [0.5, -0.5],
    })
    assert status == 201, version
    print(f"model {model_id} version {version['version']} registered "
          f"(dataset digest {version['dataset_sha256'][:12]}…)")

    # 4. Run inference on the CPU forward pass.
    status, out = call("POST", f"/models/{model_id}/versions/1/predict",
                       {"inputs": [[2.0, 1.0, 0.0], [0.0, 0.0, 0.0]]})
    assert status == 200, out
    for p in out["predictions"]:
        print(f"  -> {p['label']:11s} probs={[round(x, 4) for x in p['probabilities']]}")

    # 5. Record an experiment and compare versions.
    status, exp = call("POST", "/experiments", {
        "name": "baseline",
        "model_version_id": version["id"],
        "dataset_id": ds_id,
        "metrics": {"accuracy": 0.91},
    })
    assert status == 201, exp

    status, v2 = call("POST", f"/models/{model_id}/versions", {
        "dataset_id": ds_id,
        "input_dim": 3,
        "labels": ["setosa", "versicolor"],
        "weights": [[2.0, 0.0, -2.0], [0.0, 2.0, 2.0]],
        "bias": [0.5, -0.5],
    })
    assert status == 201, v2
    call("POST", "/experiments", {
        "name": "scaled-weights",
        "model_version_id": v2["id"],
        "dataset_id": ds_id,
        "metrics": {"accuracy": 0.93},
    })

    status, cmp_ = call("GET", f"/models/{model_id}/compare?versions=1,2")
    assert status == 200, cmp_
    print(f"compare v1 vs v2: comparable={cmp_['comparable']} "
          f"diffs={cmp_.get('metric_diffs')}")


if __name__ == "__main__":
    main()
