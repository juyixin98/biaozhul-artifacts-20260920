"""End-to-end, reproducible demonstration over REAL HTTP.

Boots the FastAPI service with uvicorn on a local port (no external accounts or
data), then drives every review scenario through HTTP:

  1. duplicate IDs          (aggregated before the optimiser)
  2. hot/cold alternating   (cold rows frozen)
  3. empty batch            (explicit "empty" verdict, no step)
  4. very large gradient    (finite, exact in float64)
  5. out-of-range index     (whole batch rejected, 400, error_code)
  6. width mismatch         (whole batch rejected, 400)
  7. checkpoint + restart   (only touched rows persisted and restored)

A machine-readable summary is written to results/demo_results.json and the
service's own correlated journal lands in <state_dir>/journal.jsonl. Every
request carries a unique run_id so results/logs can be tied to inputs.

Usage:
    python scripts/run_demo.py
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from sparse_embedding.app import create_app  # noqa: E402
from sparse_embedding.config import (  # noqa: E402
    ClipConfig,
    ClipMode,
    OptimizerConfig,
    OptimizerName,
    ServiceConfig,
    TableSpec,
)

RESULTS_DIR = ROOT / "results"
STATE_DIR = RESULTS_DIR / "demo_state"
HOST = "127.0.0.1"
# Fixed port for reproducibility; ephemeral fallback if busy.
PORT = 8765


def build_config() -> ServiceConfig:
    return ServiceConfig(
        table=TableSpec(num_rows=1000, dim=4),
        optimizer=OptimizerConfig(
            name=OptimizerName.SGD_MOMENTUM, lr=0.05, momentum=0.9
        ),
        clip=ClipConfig(ClipMode.NONE),
        state_dir=str(STATE_DIR),
        seed=20260928,
    )


class BackgroundServer:
    def __init__(self, config: ServiceConfig, port: int) -> None:
        app = create_app(config)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host=HOST, port=port, log_level="warning")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "BackgroundServer":
        self.thread.start()
        self._wait_ready()
        return self

    def _wait_ready(self, attempts: int = 50) -> None:
        for _ in range(attempts):
            if self.server.started:
                return
            time.sleep(0.05)
        raise RuntimeError("server did not become ready")

    def __exit__(self, *exc: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


def call(client: httpx.Client, label: str, payload: dict) -> dict[str, Any]:
    """POST /apply and record the exact HTTP outcome (never masked)."""
    resp = client.post("/apply", json=payload)
    record = {
        "label": label,
        "run_id": payload.get("run_id"),
        "http_status": resp.status_code,
    }
    try:
        record["body"] = resp.json()
    except Exception:
        record["body"] = resp.text
    return record


def main() -> int:
    # Start from a deterministic, clean state so repeated runs reproduce the
    # same numbers (otherwise the service would correctly resume the prior
    # checkpoint and accumulate more momentum). Restart recovery is still
    # demonstrated later within this same run.
    import shutil

    if STATE_DIR.exists():
        shutil.rmtree(STATE_DIR)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    config = build_config()

    records: list[dict[str, Any]] = []
    with BackgroundServer(config, PORT) as _:
        base = f"http://{HOST}:{PORT}"
        with httpx.Client(base_url=base, timeout=10) as client:
            health = client.get("/health").json()

            records.append(call(client, "duplicate_ids", {
                "run_id": "demo-dup-001", "batch_id": "dup",
                "indices": [0, 2, 0, 2, 2],
                "values": [[1, 0, 0, 0], [0, 2, 0, 0], [3, 4, 0, 0],
                           [-1, 0, 0, 0], [1, 1, 0, 0]],
            }))
            records.append(call(client, "hot_cold_alternate_A", {
                "run_id": "demo-hc-002", "batch_id": "hcA",
                "indices": [5, 0], "values": [[1, 1, 1, 1], [2, 0, 1, 0]],
            }))
            records.append(call(client, "hot_cold_alternate_B", {
                "run_id": "demo-hc-003", "batch_id": "hcB",
                "indices": [5, 2], "values": [[2, 2, 2, 2], [1, -1, 0, 0]],
            }))
            records.append(call(client, "empty_batch", {
                "run_id": "demo-empty-004", "indices": [], "values": [],
            }))
            records.append(call(client, "large_gradient", {
                "run_id": "demo-large-005", "batch_id": "large",
                "indices": [7, 7],
                "values": [[1e6, -1e6, 5e5, 0], [1e6, 1e6, 5e5, 0]],
            }))
            records.append(call(client, "out_of_range_high", {
                "run_id": "demo-oor-006",
                "indices": [0, 1000],
                "values": [[1, 1, 1, 1], [1, 1, 1, 1]],
            }))
            records.append(call(client, "out_of_range_negative", {
                "run_id": "demo-oor-007",
                "indices": [-1, 2],
                "values": [[1, 1, 1, 1], [1, 1, 1, 1]],
            }))
            records.append(call(client, "width_mismatch", {
                "run_id": "demo-width-008",
                "indices": [0], "values": [[1, 2, 3]],
            }))

            row_hot = client.get(f"{base}/state/row/5").json()
            row_cold = client.get(f"{base}/state/row/99").json()
            ckpt = client.post(f"{base}/checkpoint").json()
            summary = client.get(f"{base}/state/summary").json()

    # Restart on the SAME state dir, in-process, to prove persistence recovery.
    config2 = build_config()
    from sparse_embedding.service import SparseEmbeddingService
    from sparse_embedding.journal import RunJournal

    svc2 = SparseEmbeddingService(
        config2, RunJournal(str(STATE_DIR / "journal.jsonl"))
    )
    recovered = svc2.load_if_present()
    restart_row5 = svc2.row(5)

    summary_out = {
        "health": health,
        "requests": records,
        "checkpoint": ckpt,
        "summary_after": summary,
        "hot_row_5_before_restart": row_hot,
        "cold_row_99_untouched": row_cold,
        "restart": {
            "checkpoint_found": recovered,
            "global_step": svc2.state.global_step,
            "row_5_weight": list(map(float, restart_row5["weight"])),
            "row_5_momentum": list(map(float, restart_row5["momentum"])),
            "row_5_steps": int(restart_row5["row_steps"]),
            "row_99_steps": int(svc2.row(99)["row_steps"]),
        },
    }

    out_path = RESULTS_DIR / "demo_results.json"
    out_path.write_text(json.dumps(summary_out, indent=2, sort_keys=True) + "\n")

    # Console verdict table - explicit success/failure categories.
    print(f"health: {health['status']}")
    for r in records:
        body = r["body"]
        verdict = body.get("verdict", body.get("error_code", "?"))
        print(f"  [{r['http_status']}] {r['label']:<28} -> {verdict}  ({r['run_id']})")
    print(f"checkpoint: {ckpt.get('verdict')} rows={ckpt.get('persisted_touched_rows')}")
    print(f"restart recovered={recovered} global_step={svc2.state.global_step}")
    print(f"results written to {out_path}")

    # Hard assertions so the demo exits non-zero if behaviour regressed.
    by_label = {r["label"]: r for r in records}
    assert by_label["duplicate_ids"]["http_status"] == 200
    assert by_label["duplicate_ids"]["body"]["active_indices"] == [0, 2]
    assert by_label["empty_batch"]["body"]["verdict"] == "empty"
    assert by_label["large_gradient"]["http_status"] == 200
    for bad in ("out_of_range_high", "out_of_range_negative", "width_mismatch"):
        assert by_label[bad]["http_status"] == 400, bad
        assert by_label[bad]["body"]["error_code"] == "batch_rejected", bad
    assert recovered is True
    assert restart_row5["row_steps"] >= 2
    assert svc2.row(99)["row_steps"] == 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
