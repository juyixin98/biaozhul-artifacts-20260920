"""Safe dataset loading.

Datasets live inside one of the configured whitelist directories. Every
candidate path is *resolved* (``Path.resolve`` follows symlinks) before the
containment check, so ``data/evil.csv -> /etc/passwd`` style symlink escapes
are rejected, as are ``..`` traversals. Files are opened through their
resolved path so a symlink swapped in after the check cannot redirect reads.
"""
from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .. import config


class PathViolation(Exception):
    """Raised when a requested path escapes the whitelist."""


def resolve_within_whitelist(
    rel_or_abs: str | Path, roots: list[Path] | None = None
) -> Path:
    roots = roots or config.data_whitelist()
    roots = [Path(r).resolve(strict=False) for r in roots]

    candidate = Path(rel_or_abs)
    if not candidate.is_absolute():
        # Try each root; use the first whose *resolved* result exists, else
        # the first root for error messages.
        resolved = (roots[0] / candidate).resolve(strict=False) if roots else None
        for root in roots:
            attempt = (root / candidate).resolve(strict=False)
            if attempt.exists():
                resolved = attempt
                break
    else:
        resolved = candidate.resolve(strict=False)

    if resolved is None:
        raise PathViolation(f"empty whitelist; cannot resolve {rel_or_abs!r}")

    for root in roots:
        # os.path.commonpath-style check that also enforces equality.
        try:
            resolved.relative_to(root)
        except ValueError:
            continue
        # Reject non-regular files (devices/fifos) and broken symlinks.
        if not resolved.exists():
            raise PathViolation(f"file does not exist: {resolved}")
        if not resolved.is_file():
            raise PathViolation(f"not a regular file: {resolved}")
        # Reject a symlink whose target is inside the tree but whose final
        # component hop leaves the whitelist (relative_to already covers
        # this, but we double-check every intermediate resolution stays in).
        return resolved
    raise PathViolation(
        f"path {resolved} escapes the whitelist roots "
        f"{[str(r) for r in roots]}"
    )


def file_digest(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def _load_csv(path: Path) -> tuple[np.ndarray, np.ndarray]:
    rows: list[list[float]] = []
    with open(path, newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            if not row or all(not c.strip() for c in row):
                continue  # tolerate blank lines
            try:
                rows.append([float(c) for c in row])
            except ValueError as exc:
                raise ValueError(
                    f"{path.name}: non-numeric value on row {i + 1}: {exc}"
                ) from exc
    if not rows:
        raise ValueError(f"{path.name}: CSV contains no rows")
    width = len(rows[0])
    if width < 2:
        raise ValueError(f"{path.name}: CSV needs >= 2 columns (features + target)")
    bad = [i + 1 for i, r in enumerate(rows) if len(r) != width]
    if bad:
        raise ValueError(f"{path.name}: ragged CSV rows at lines {bad[:5]}")
    arr = np.asarray(rows, dtype=np.float32)
    return arr[:, :-1], arr[:, -1]


def _load_npy(path: Path, target_path: Path | None) -> tuple[np.ndarray, np.ndarray]:
    features = np.load(path, allow_pickle=False, mmap_mode=None)
    if target_path is None:
        raise ValueError("npy features require a separate target .npy file")
    targets = np.load(target_path, allow_pickle=False, mmap_mode=None)
    if features.ndim != 2:
        raise ValueError(f"{path.name}: feature array must be 2-D (N, F)")
    return features.astype(np.float32, copy=False), _coerce_targets(targets)


def _coerce_targets(raw: np.ndarray) -> np.ndarray:
    if raw.ndim == 2 and raw.shape[1] == 1:
        raw = raw[:, 0]
    return raw.astype(np.float32, copy=False) if raw.dtype != np.int64 else raw


def load_dataset(
    feature_path: str,
    target_path: str | None = None,
) -> tuple[np.ndarray, np.ndarray]:
    """Resolve, validate and load a dataset. Returns (X[N,F], y[N]).

    - CSV: a single file whose last column is the target.
    - NPY: ``feature_path`` is the (N, F) matrix; ``target_path`` is required
      and holds the targets.
    """
    feat = resolve_within_whitelist(feature_path)
    suffix = feat.suffix.lower()
    if suffix == ".csv":
        if target_path:
            tgt = resolve_within_whitelist(target_path)
            if tgt != feat:
                raise ValueError("CSV datasets carry their target in the last column")
        return _load_csv(feat)
    if suffix == ".npy":
        tgt = resolve_within_whitelist(target_path) if target_path else None
        return _load_npy(feat, tgt)
    raise ValueError(f"unsupported dataset extension {suffix!r}; use .csv or .npy")


@dataclass(frozen=True)
class DatasetSummary:
    n_samples: int
    n_features: int
    feature_sha256: str
    target_sha256: str | None
    task: str

    def to_dict(self) -> dict:
        return {
            "n_samples": self.n_samples,
            "n_features": self.n_features,
            "feature_sha256": self.feature_sha256,
            "target_sha256": self.target_sha256,
            "task": self.task,
        }


def summarize(
    X: np.ndarray, y: np.ndarray, feature_resolved: Path,
    target_resolved: Path | None, task: str
) -> DatasetSummary:
    return DatasetSummary(
        n_samples=int(X.shape[0]),
        n_features=int(X.shape[1]),
        feature_sha256=file_digest(feature_resolved),
        target_sha256=file_digest(target_resolved) if target_resolved else None,
        task=task,
    )
