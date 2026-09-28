"""Computational graph: sparse aggregation and clipping primitives.

Layer 2. These functions are pure (inputs in, outputs out, no state mutation)
so they can be unit-tested and independently compared against a dense
reference. They contain no I/O and no training-step logic.
"""

from __future__ import annotations

import numpy as np

from .config import ClipConfig, ClipMode
from .tensors import AggregatedSparseGradient, SparseGradientBatch


def aggregate_duplicates(batch: SparseGradientBatch) -> AggregatedSparseGradient:
    """Coalesce repeated indices by summing their gradient rows.

    Contract:

    * The returned indices are unique and sorted ascending.
    * Each output row is the element-wise sum of ALL raw rows that targeted
      that index — duplicates are aggregated before any optimiser sees them.
    * Rows whose summed gradient is exactly zero are *kept* here (they are a
      real touch target); the training layer applies the explicit "zero
      gradient takes no step" rule.

    Uses ``np.add.at`` (unbuffered scatter-add), which is the correct primitive
    when the same index repeats — a plain fancy-assignment would keep only the
    last row and silently drop gradient mass.
    """

    batch.require_non_empty()

    unique_indices, inverse = np.unique(batch.indices, return_inverse=True)
    unique_indices = unique_indices.astype(batch.indices.dtype, copy=False)

    aggregated = np.zeros(
        (unique_indices.shape[0], batch.dim), dtype=batch.values.dtype
    )
    # Unbuffered: repeated indices accumulate rather than overwrite.
    np.add.at(aggregated, inverse, batch.values)

    row_norms = np.linalg.norm(aggregated, ord=2, axis=1)
    global_norm = float(np.linalg.norm(aggregated, ord="fro"))

    return AggregatedSparseGradient(
        unique_indices=unique_indices,
        aggregated=aggregated,
        row_norms=row_norms,
        global_norm=global_norm,
        raw_count=batch.size,
    )


def clip_gradients(
    agg: AggregatedSparseGradient, clip: ClipConfig
) -> tuple[np.ndarray, dict]:
    """Return a clipped copy plus an auditable clipping report.

    Global and row-wise clipping are mutually exclusive code paths selected by
    the declared ``clip.mode``; they are never blended.

    * ``GLOBAL``: ``scale = min(1, max_norm / global_norm)`` applied uniformly
      to every row. If the global norm <= max_norm nothing changes (scale 1).
    * ``ROW``: one ``scale_k = min(1, max_norm / row_norm_k)`` per row.
    * ``NONE``: identity.

    A zero norm yields scale 1 (0/max_norm clamps via min with 1, and dividing
    zero by zero is avoided explicitly). The returned arrays are fresh copies;
    the aggregated gradient is never mutated.
    """

    g = agg.aggregated
    report: dict = {"mode": clip.mode.value, "applied": False}

    if clip.mode is ClipMode.NONE:
        report["scale"] = 1.0
        return g.copy(), report

    assert clip.max_norm is not None  # guaranteed by ClipConfig validation
    max_norm = float(clip.max_norm)

    if clip.mode is ClipMode.GLOBAL:
        scale = 1.0 if agg.global_norm <= max_norm else max_norm / agg.global_norm
        out = g * scale
        report.update(
            applied=scale < 1.0,
            scale=float(scale),
            pre_norm=agg.global_norm,
            post_norm=float(np.linalg.norm(out, ord="fro")),
            max_norm=max_norm,
        )
        return out, report

    if clip.mode is ClipMode.ROW:
        safe_norm = np.where(agg.row_norms > 0.0, agg.row_norms, 1.0)
        scales = np.minimum(1.0, max_norm / safe_norm)
        # Zero-norm rows get scale 1 (multiplies zeros anyway); guard the
        # 0/0 case explicitly.
        scales = np.where(agg.row_norms > 0.0, scales, 1.0)
        out = g * scales[:, None]
        report.update(
            applied=bool((scales < 1.0).any()),
            scales=scales.tolist(),
            pre_row_norms=agg.row_norms.tolist(),
            post_row_norms=np.linalg.norm(out, ord=2, axis=1).tolist(),
            max_norm=max_norm,
        )
        return out, report

    # Unreachable: ClipMode is a closed enum, but fail loudly rather than
    # silently return unclipped gradients if extended incorrectly.
    raise ValueError(f"unsupported clip mode: {clip.mode!r}")
