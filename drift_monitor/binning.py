"""Fixed binning with underflow / overflow / missing buckets.

Boundaries are learned once on the baseline window (quantile edges by
default) and then frozen: the exact same boundaries are applied to every
current window, which is what makes the resulting counts comparable.

The model holds ``n_bins + 1`` full edges ``[e0, ..., en]`` and bucket
indices are laid out as follows (``n_bins`` finite buckets + 3 specials)::

    [0]          underflow   x <  e0
    [1 .. n]     finite      e[k-1] <= x < e[k]   (top bucket includes en)
    [n + 1]      overflow    x >  en
    [n + 2]      missing     x is NaN

Quantile edges can tie on heavily-tied baseline data; zero-width finite
buckets are kept (they simply stay empty) so the bucket count never
changes between baseline and current windows.
"""
from __future__ import annotations

import numpy as np

MISSING_LABEL = "missing"

# +/-inf edges (allowed for custom open-ended bins) are mapped to finite
# sentinels internally so searchsorted stays well defined.
_INF_EDGE = 1e308


def _sanitize_edges(edges: np.ndarray) -> np.ndarray:
    """Validate user-supplied edges and map +/-inf to finite sentinels."""
    edges = np.asarray(edges, dtype=np.float64)
    if edges.ndim != 1 or edges.size < 2:
        raise ValueError("edges must be a 1-D array with at least two values")
    if np.any(np.isnan(edges)):
        raise ValueError("edges must not contain NaN")
    edges = np.where(edges == -np.inf, -_INF_EDGE, edges)
    edges = np.where(edges == np.inf, _INF_EDGE, edges)
    if np.any(np.diff(edges) < 0):
        raise ValueError("edges must be non-decreasing")
    return edges


class FixedBinner:
    """Frozen boundaries for one numeric feature.

    Parameters
    ----------
    n_bins:
        Number of finite buckets (plus the 3 special buckets).
    strategy:
        ``"quantile"`` (equal-frequency on the baseline, recommended for
        PSI), ``"uniform"`` (equal-width) or ``"custom"``.
    edges:
        Required for ``strategy="custom"``: the full boundary array of
        length ``n_bins + 1``, non-decreasing; +/-inf create open-ended
        finite buckets.
    """

    def __init__(self, n_bins: int = 10, strategy: str = "quantile",
                 edges: np.ndarray | None = None) -> None:
        if int(n_bins) < 1:
            raise ValueError("n_bins must be >= 1")
        if strategy not in ("quantile", "uniform", "custom"):
            raise ValueError(f"unknown strategy: {strategy!r}")
        self.n_bins = int(n_bins)
        self.strategy = strategy
        self.edges: np.ndarray | None = None
        if strategy == "custom":
            if edges is None:
                raise ValueError("strategy='custom' requires edges")
            self.edges = _sanitize_edges(edges)
            if self.edges.size != self.n_bins + 1:
                raise ValueError(
                    f"expected {self.n_bins + 1} full edges for "
                    f"{self.n_bins} finite bins, got {self.edges.size}"
                )

    @property
    def n_buckets(self) -> int:
        return self.n_bins + 3  # underflow + overflow + missing

    # ------------------------------------------------------------------ fit
    def fit(self, values: np.ndarray) -> "FixedBinner":
        """Learn boundaries on baseline values (NaN ignored)."""
        if self.strategy == "custom":
            return self
        x = np.asarray(values, dtype=np.float64).reshape(-1)
        finite = x[~np.isnan(x)]
        if finite.size == 0:
            raise ValueError("cannot fit bins on an all-missing / empty baseline")
        if self.strategy == "quantile":
            if np.all(finite == finite[0]):
                # All quantiles collapse; widen to a symmetric epsilon window
                # so identical values bin inside and shifted ones overflow.
                c = float(finite[0])
                eps = max(abs(c) * 1e-9, 1e-12)
                self.edges = np.linspace(c - eps, c + eps, self.n_bins + 1)
            else:
                qs = np.linspace(0.0, 1.0, self.n_bins + 1)
                self.edges = np.quantile(finite, qs).astype(np.float64)
        else:  # uniform
            if np.all(finite == finite[0]):
                c = float(finite[0])
                eps = max(abs(c) * 1e-9, 1e-12)
                lo, hi = c - eps, c + eps
            else:
                lo, hi = float(finite.min()), float(finite.max())
            self.edges = np.linspace(lo, hi, self.n_bins + 1)
        return self

    # ------------------------------------------------------------- transform
    def _bucket_index(self, x: np.ndarray) -> np.ndarray:
        """Map finite values to bucket indices 0 .. n_bins + 1.

        ``searchsorted(..., side='right')`` over the n+1 edges follows the
        same convention as ``np.digitize`` (left-closed, right-open):

            j = 0        -> x <  e[0]  -> underflow
            j = 1 .. n   -> e[j-1] <= x < e[j] -> finite bucket j
            j = n + 1    -> x >= e[n]; the value x == e[n] is clamped into
                            finite bucket n (top bucket includes its right
                            edge), strictly larger values stay in overflow.
        """
        idx = np.searchsorted(self.edges, x, side="right")
        idx = np.where(x == self.edges[-1], self.n_bins, idx)
        return idx

    def transform(self, values: np.ndarray) -> np.ndarray:
        """Return bucket index for every entry; NaN -> missing bucket."""
        assert self.edges is not None, "binner must be fitted before use"
        x = np.asarray(values, dtype=np.float64).reshape(-1)
        out = np.full(x.shape, self.n_bins + 2, dtype=np.int64)  # missing
        mask = ~np.isnan(x)
        if mask.any():
            out[mask] = self._bucket_index(x[mask])
        return out

    def counts(self, values: np.ndarray) -> np.ndarray:
        """Raw per-bucket counts for a window (length ``n_buckets``)."""
        idx = self.transform(values)
        return np.bincount(idx, minlength=self.n_buckets).astype(np.int64)

    # ------------------------------------------------------------- reporting
    def bucket_edges(self) -> list[tuple[float | None, float | None]]:
        """Finite-bucket ranges ``(lower_inclusive, upper)`` for buckets 1..n."""
        assert self.edges is not None, "binner must be fitted before use"
        return [(float(self.edges[k - 1]), float(self.edges[k]))
                for k in range(1, self.n_bins + 1)]

    def to_dict(self) -> dict:
        assert self.edges is not None, "binner must be fitted before serialization"
        return {
            "n_bins": self.n_bins,
            "strategy": self.strategy,
            "edges": [float(e) for e in self.edges],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> "FixedBinner":
        obj = cls(n_bins=int(payload["n_bins"]), strategy=payload["strategy"])
        obj.edges = _sanitize_edges(np.asarray(payload["edges"], dtype=np.float64))
        return obj


def bucket_labels(binner: FixedBinner) -> list[str]:
    """Stable string labels aligned with the count/probability vectors."""
    assert binner.edges is not None, "binner must be fitted before use"
    labels = ["underflow"]
    for k in range(1, binner.n_bins + 1):
        lo = binner.edges[k - 1]
        hi = binner.edges[k]
        lo_s = "-inf" if lo <= -_INF_EDGE / 2 else f"{float(lo):.6g}"
        hi_s = "+inf" if hi >= _INF_EDGE / 2 else f"{float(hi):.6g}"
        close = "]" if k == binner.n_bins else ")"
        labels.append(f"[{lo_s}, {hi_s}{close}")
    labels += ["overflow", MISSING_LABEL]
    return labels
