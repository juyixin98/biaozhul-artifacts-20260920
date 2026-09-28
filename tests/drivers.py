"""Test-only driver helpers.

These helpers drive the *production* coordinator the way a real control
plane would, but the expected answers always come from
:mod:`bucket_sync.reference`, never from the coordinator itself.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from bucket_sync.bucketing import BucketLayout
from bucket_sync.coordinator import Coordinator
from bucket_sync.training import linear_mse_gradients

GradSums = Dict[str, np.ndarray]


def register_all(coordinator: Coordinator, worker_ids: Sequence[str]) -> None:
    for wid in worker_ids:
        coordinator.register_worker(wid)


def local_gradient_sums(
    base_params: Dict[str, np.ndarray],
    shard: Tuple[np.ndarray, np.ndarray],
    *,
    drop_params: Sequence[str] = (),
) -> Tuple[GradSums, int]:
    grads, n = linear_mse_gradients(base_params, shard[0], shard[1])
    for name in drop_params:
        grads.pop(name, None)
    return grads, n


def coverage_flat(layout: BucketLayout, grads: GradSums) -> np.ndarray:
    """True exactly on trainable slots the worker computed gradients for."""
    mask = np.zeros(layout.flat_size, dtype=bool)
    for name in grads:
        node = layout.graph.node(name)
        if not node.trainable:
            continue
        sl = layout.slice_for(name)
        mask[sl.offset : sl.offset + sl.size] = True
    return mask


def bucket_segments(
    layout: BucketLayout, grads: GradSums
) -> Dict[int, Tuple[np.ndarray, np.ndarray]]:
    """Per bucket: ``(segment, present_mask)`` from per-param gradient sums.

    Gradients for non-trainable parameters are dropped: a worker never
    transmits a gradient for a frozen slot; that slot remains an explicit
    zero placeholder in the fixed layout.
    """
    trainable = set(layout.graph.trainable_names())
    grads = {k: v for k, v in grads.items() if k in trainable}
    flat = layout.pack_flat(grads)
    present = coverage_flat(layout, grads)
    out: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    for b in layout.buckets:
        out[b.index] = (
            flat[b.start : b.end].copy(),
            present[b.start : b.end].copy(),
        )
    return out


def submit_round(
    coordinator: Coordinator,
    worker_ids: Sequence[str],
    shards: Sequence[Tuple[np.ndarray, np.ndarray]],
    layout: BucketLayout,
    *,
    orders: Optional[Dict[str, List[int]]] = None,
    drop_params: Sequence[str] = (),
) -> Tuple[object, Dict[str, Tuple[GradSums, int]]]:
    """Register, begin, and submit every bucket; does NOT commit.

    Returns the round descriptor and each worker's ``(grad_sums, n)``.
    """
    register_all(coordinator, worker_ids)
    desc = coordinator.begin_round(list(worker_ids))
    base_params = desc["base_params"]
    token = desc["base_token"]
    locals_: Dict[str, Tuple[GradSums, int]] = {}
    for wid, shard in zip(worker_ids, shards):
        grads, n = local_gradient_sums(
            base_params, shard, drop_params=drop_params
        )
        locals_[wid] = (grads, n)
        segments = bucket_segments(layout, grads)
        order = (orders or {}).get(wid, list(segments))
        for bi in order:
            segment, mask = segments[bi]
            coordinator.submit_bucket(
                round_id=desc["round_id"],
                worker_id=wid,
                bucket_index=bi,
                segment=segment,
                n_samples=n,
                base_token=token,
                present_mask=mask,
            )
    return desc, locals_
