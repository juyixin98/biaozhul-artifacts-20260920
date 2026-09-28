"""Documented, independently implemented dropout seeding rule.

The system under test (:mod:`app.core.rng`) must reproduce exactly these
masks; this module does not import it.

Two documented strategies
-------------------------

counter (default)
    Each dropout node ``i`` owns ``numpy.random.default_rng`` seeded by
    ``SeedSequence([master_seed, H(i)])`` where ``H(i)`` is the first 4
    bytes of ``sha256(i)`` interpreted big-endian.  The first random draw
    for the node determines its mask; replay simply recreates the generator.

snapshot
    A single ``numpy.random.RandomState(master_seed)`` is created before the
    forward; dropout nodes draw in graph order using ``random_sample``.
    Replay restores the captured state and skips earlier draws.

Mask convention is inverted dropout: ``keep = 1-p`` and
``mask = (u < keep) / keep`` for uniform draws ``u``.
"""

from __future__ import annotations

import hashlib
from typing import Dict, List, Tuple

import numpy as np


def hash_node_id(node_id: str) -> int:
    return int.from_bytes(hashlib.sha256(node_id.encode()).digest()[:4], "big")


def counter_mask(master_seed: int, node_id: str, shape: Tuple[int, ...],
                 p: float) -> np.ndarray:
    gen = np.random.default_rng(
        np.random.SeedSequence([master_seed, hash_node_id(node_id)])
    )
    keep = 1.0 - p
    return (gen.random(shape) < keep).astype(np.float64) / keep


def snapshot_masks(master_seed: int, dropout_order: List[str],
                   shapes: Dict[str, Tuple[int, ...]], p_by_node: Dict[str, float]
                   ) -> Dict[str, np.ndarray]:
    rs = np.random.RandomState(master_seed)
    masks: Dict[str, np.ndarray] = {}
    for nid in dropout_order:
        keep = 1.0 - p_by_node[nid]
        masks[nid] = (
            rs.random_sample(shapes[nid]) < keep
        ).astype(np.float64) / keep
    return masks


def reference_masks(strategy: str, master_seed: int, spec: dict
                    ) -> Dict[str, np.ndarray]:
    """Build the fixed mask set for a fixture spec."""

    order = [n["id"] for n in spec["nodes"]]
    shapes = {n["id"]: tuple(n["params"]["shape"])
              for n in spec["nodes"] if n["op"] in ("input", "parameter")}
    # Output shapes by a minimal local inference (linear/add/relu/dropout/
    # external preserve the rules used by the fixtures).
    for n in spec["nodes"]:
        if n["op"] == "linear":
            x_id, w_id = n["inputs"][0], n["inputs"][1]
            b = shapes[x_id][0]
            ncol = shapes[w_id][1]
            shapes[n["id"]] = (b, ncol)
        elif n["op"] == "reduce_sum":
            shapes[n["id"]] = (1,)
        elif n["id"] not in shapes:
            shapes[n["id"]] = shapes[n["inputs"][0]]
    dropouts = [nid for nid in order
                if next(n for n in spec["nodes"] if n["id"] == nid)["op"]
                == "dropout"]
    p_by_node = {
        nid: next(n for n in spec["nodes"] if n["id"] == nid)["params"]["p"]
        for nid in dropouts
    }
    if strategy == "counter":
        return {
            nid: counter_mask(master_seed, nid, shapes[nid], p_by_node[nid])
            for nid in dropouts
        }
    if strategy == "snapshot":
        return snapshot_masks(master_seed, dropouts, shapes, p_by_node)
    raise ValueError(f"unknown strategy {strategy!r}")
