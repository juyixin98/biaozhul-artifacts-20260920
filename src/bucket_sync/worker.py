"""Worker subprocess: computes local gradients and submits buckets.

A worker is an OS process talking to the coordinator over HTTP.  It owns
only its local data shard; it never sees other workers' data or the
reduced result.  Per round it:

1. fetches the round descriptor (round id, base token, base params),
2. recomputes gradient *sums* on its shard from those exact base params,
3. packs them into the fixed flat layout and slices out buckets,
4. submits buckets in a deterministic-but-arbitrary completion order
   (shuffled by a per-round seed, to prove order-independence),
5. optionally simulates a crash (``crash_after_buckets``) to exercise the
   lost-worker path.

Fault-injection knobs live in :class:`WorkerConfig` and are plain data so
tests can drive every edge case without monkey-patching.
"""

from __future__ import annotations

import json
import time
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional

import numpy as np

from bucket_sync.bucketing import BucketLayout
from bucket_sync.training import linear_mse_gradients


@dataclass(frozen=True)
class WorkerConfig:
    worker_id: str
    server_url: str
    layout: BucketLayout
    x_shard: np.ndarray
    y_shard: np.ndarray
    # fault injection / behavior knobs
    submit_order_seed: int = 0
    crash_after_buckets: Optional[int] = None  # die mid-round after N buckets
    hang_after_buckets: Optional[int] = None  # freeze (no heartbeats) after N
    corrupt_base_token: bool = False  # submit with a stale/wrong base token
    missing_params: tuple = ()  # params for which no gradient is produced
    poll_interval_s: float = 0.05
    heartbeat_interval_s: float = 0.2
    max_round_wait_s: float = 30.0
    hang_duration_s: float = 120.0  # how long a simulated freeze lasts


class WorkerHttpError(RuntimeError):
    pass


def _post(url: str, payload: dict, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _get(url: str, timeout: float = 10.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _to_arrays(params_json: Dict[str, list]) -> Dict[str, np.ndarray]:
    return {k: np.asarray(v, dtype=np.float64) for k, v in params_json.items()}


def run_worker(cfg: WorkerConfig, rounds: int) -> List[dict]:
    """Worker main loop; returns one summary dict per completed round.

    This function is the subprocess entry point (must stay picklable-safe:
    only module-level, config passed in).
    """
    base = cfg.server_url.rstrip("/")
    _post(f"{base}/workers/{cfg.worker_id}/register", {})
    summaries: List[dict] = []
    last_heartbeat = 0.0
    seen_rounds = 0

    while seen_rounds < rounds:
        now = time.monotonic()
        if now - last_heartbeat >= cfg.heartbeat_interval_s:
            try:
                _post(f"{base}/workers/{cfg.worker_id}/heartbeat", {})
            except Exception:
                pass
            last_heartbeat = now

        desc = _get(f"{base}/rounds/current")
        if not desc.get("open"):
            time.sleep(cfg.poll_interval_s)
            continue
        if cfg.worker_id not in desc.get("participants", []):
            time.sleep(cfg.poll_interval_s)
            continue

        round_id = desc["round_id"]
        base_token = desc["base_token"]
        base_params = _to_arrays(desc["base_params"])

        grads, n_samples = linear_mse_gradients(base_params, cfg.x_shard, cfg.y_shard)
        for missing in cfg.missing_params:
            grads.pop(missing, None)
        # Never transmit gradients for non-trainable parameters; their slots
        # stay explicit zero placeholders in the fixed layout.
        trainable = set(cfg.layout.graph.trainable_names())
        grads = {k: v for k, v in grads.items() if k in trainable}
        flat = cfg.layout.pack_flat(grads)

        # Explicit coverage: True exactly on trainable slots of params this
        # worker actually computed.  Missing gradients are declared, not
        # silently sent as zeros.
        present_flat = np.zeros(cfg.layout.flat_size, dtype=bool)
        graph = cfg.layout.graph
        for name in grads:
            node = graph.node(name)
            if not node.trainable:
                continue
            sl = cfg.layout.slice_for(name)
            present_flat[sl.offset : sl.offset + sl.size] = True

        order = list(range(cfg.layout.bucket_count()))
        rng = np.random.default_rng(cfg.submit_order_seed + round_id)
        rng.shuffle(order)

        token_to_send = "bogus-token" if cfg.corrupt_base_token else base_token
        accepted: List[int] = []
        rejected: List[dict] = []
        crashed = False
        for i, bucket_index in enumerate(order):
            if cfg.crash_after_buckets is not None and i >= cfg.crash_after_buckets:
                crashed = True  # simulated sudden death: no cleanup, no more heartbeats
                break
            if cfg.hang_after_buckets is not None and i >= cfg.hang_after_buckets:
                # Simulated freeze / network partition: process is alive but
                # makes no progress and sends no heartbeats.
                summaries.append(
                    {
                        "worker_id": cfg.worker_id,
                        "round_id": round_id,
                        "n_samples": n_samples,
                        "accepted_buckets": accepted,
                        "rejected": rejected,
                        "crashed": False,
                        "hung": True,
                    }
                )
                seen_rounds += 1
                time.sleep(cfg.hang_duration_s)
                return summaries
            bucket = cfg.layout.buckets[bucket_index]
            segment = flat[bucket.start : bucket.end]
            mask = present_flat[bucket.start : bucket.end]
            resp = _post(
                f"{base}/buckets/submit",
                {
                    "round_id": round_id,
                    "worker_id": cfg.worker_id,
                    "bucket_index": bucket_index,
                    "segment": segment.tolist(),
                    "present_mask": mask.tolist(),
                    "n_samples": n_samples,
                    "base_token": token_to_send,
                },
            )
            if resp.get("accepted"):
                accepted.append(bucket_index)
            else:
                rejected.append(
                    {"bucket": bucket_index, "reason": resp.get("reason")}
                )
        summaries.append(
            {
                "worker_id": cfg.worker_id,
                "round_id": round_id,
                "n_samples": n_samples,
                "accepted_buckets": accepted,
                "rejected": rejected,
                "crashed": crashed,
            }
        )
        seen_rounds += 1
        if crashed:
            return summaries
        # wait until this round closes before looking for the next one;
        # keep heartbeating so the coordinator does not mistake a survivor
        # blocked at the commit barrier for a dead worker.
        deadline = time.monotonic() + cfg.max_round_wait_s
        last_hb = time.monotonic()
        while time.monotonic() < deadline:
            if time.monotonic() - last_hb >= cfg.heartbeat_interval_s:
                try:
                    _post(f"{base}/workers/{cfg.worker_id}/heartbeat", {})
                except Exception:
                    pass
                last_hb = time.monotonic()
            d = _get(f"{base}/rounds/current")
            if not d.get("open") or d.get("round_id") != round_id:
                break
            time.sleep(cfg.poll_interval_s)
    return summaries


def worker_entry(cfg: WorkerConfig, rounds: int, result_queue) -> None:
    """multiprocessing target: run and report through a queue."""
    try:
        summaries = run_worker(cfg, rounds)
        result_queue.put({"ok": True, "worker_id": cfg.worker_id, "summaries": summaries})
    except Exception as exc:  # surfaced to driver; never swallowed
        result_queue.put(
            {"ok": False, "worker_id": cfg.worker_id, "error": f"{type(exc).__name__}: {exc}"}
        )
