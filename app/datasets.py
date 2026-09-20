"""Safe dataset loading from whitelisted directories.

Path security:
  * the candidate path is lexically normalised, symlinks are resolved with
    ``os.path.realpath`` and the *resolved* path must stay inside one of the
    whitelist roots (which are themselves realpaths) — this blocks both
    ``../`` traversal and symlink escape;
  * only regular files are accepted;
  * the extension must match the declared format.
"""
from __future__ import annotations

import csv
import hashlib
import os
from typing import Any

import numpy as np

from .config import Settings, get_settings

SUPPORTED_FORMATS = ("csv", "npy")
SUPPORTED_TASKS = ("classification", "regression")


class DatasetError(ValueError):
    pass


def safe_resolve(path: str, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    if not path or not isinstance(path, str):
        raise DatasetError("path must be a non-empty string")
    if "\x00" in path:
        raise DatasetError("invalid path")

    # Reject NUL / absolute-relative tricks up front: realpath handles
    # ".." lexically AND resolves every symlink component.
    resolved = os.path.realpath(path)
    if not os.path.isfile(resolved):
        raise DatasetError(f"dataset not found: {path!r}")
    if os.path.islink(path) and not os.path.exists(path):  # defensive
        raise DatasetError("dangling symlink")

    roots = settings.whitelist
    for root in roots:
        # commonpath would raise ValueError on mixed drives (irrelevant on
        # Linux) but is the strict containment check we want.
        if resolved == root:
            break
        if os.path.commonpath([resolved, root]) == root:
            break
    else:
        raise DatasetError(
            f"path {path!r} resolves outside the whitelisted data directories"
        )
    return resolved


def detect_format(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        return "csv"
    if ext == ".npy":
        return "npy"
    raise DatasetError(f"unsupported file type {ext!r}; expected .csv or .npy")


def file_digest(path: str, chunk_size: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(chunk_size):
            h.update(chunk)
    return h.hexdigest()


def load_csv(path: str, label_column: str | None = None) -> tuple[np.ndarray, np.ndarray]:
    with open(path, newline="") as f:
        reader = csv.reader(f)
        try:
            header = next(reader)
        except StopIteration:
            raise DatasetError("csv file is empty (header required)")
        if len(header) < 2:
            raise DatasetError("csv needs at least one feature column and a label column")
        if any(not c.strip() for c in header):
            raise DatasetError("csv header contains an empty column name")
        if len(set(header)) != len(header):
            raise DatasetError("csv header contains duplicate column names")

        if label_column is None:
            label_idx = len(header) - 1
        else:
            try:
                label_idx = header.index(label_column)
            except ValueError:
                raise DatasetError(f"label column {label_column!r} not found in header")

        features: list[list[float]] = []
        labels: list[float] = []
        for row_num, row in enumerate(reader, start=2):
            if not row:
                continue  # tolerate trailing blank lines
            if len(row) != len(header):
                raise DatasetError(
                    f"csv row {row_num} has {len(row)} fields, expected {len(header)}"
                )
            try:
                values = [float(v) for v in row]
            except ValueError:
                raise DatasetError(f"csv row {row_num} contains non-numeric data")
            labels.append(values[label_idx])
            features.append(values[:label_idx] + values[label_idx + 1 :])

    if not features:
        raise DatasetError("csv contains no data rows")
    x = np.asarray(features, dtype=np.float32)
    y = np.asarray(labels, dtype=np.float32)
    return x, y


def load_npy(path: str) -> tuple[np.ndarray, np.ndarray]:
    try:
        arr = np.load(path, allow_pickle=False)
    except Exception as exc:  # numpy raises a variety of errors on corrupt files
        raise DatasetError(f"cannot load npy file: {exc}") from exc
    if arr.ndim != 2:
        raise DatasetError(f"npy array must be 2-D, got {arr.ndim}-D")
    if arr.shape[0] < 1 or arr.shape[1] < 2:
        raise DatasetError("npy array needs at least one feature column and one label")
    x = np.ascontiguousarray(arr[:, :-1], dtype=np.float32)
    y = np.ascontiguousarray(arr[:, -1], dtype=np.float32)
    return x, y


def inspect_dataset(
    path: str,
    task: str = "classification",
    label_column: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """Resolve, validate and summarise a dataset file.

    Returns {resolved_path, fmt, num_rows, num_features, digest, x?, y?}.
    The arrays are returned for callers that train immediately; the row
    summary is what gets persisted.
    """
    if task not in SUPPORTED_TASKS:
        raise DatasetError(f"task must be one of {SUPPORTED_TASKS}")
    resolved = safe_resolve(path, settings)
    fmt = detect_format(resolved)
    if fmt == "csv":
        x, y = load_csv(resolved, label_column=label_column)
    else:
        if label_column is not None:
            raise DatasetError("label_column is only supported for csv files")
        x, y = load_npy(resolved)

    if task == "classification":
        if np.any(y != np.round(y)):
            raise DatasetError("classification labels must be integers")
        classes = np.unique(y.astype(np.int64))
        if classes.size < 2:
            raise DatasetError("classification requires at least 2 classes")
    digest = file_digest(resolved)
    return {
        "resolved_path": resolved,
        "fmt": fmt,
        "num_rows": int(x.shape[0]),
        "num_features": int(x.shape[1]),
        "digest": digest,
        "x": x,
        "y": y,
    }


def make_split(
    num_rows: int, val_fraction: float, seed: int
) -> dict[str, list[int]]:
    """Deterministic train/val split, fixed at job creation.

    The same (num_rows, val_fraction, seed) always yields the same indices —
    resuming a job reuses the indices stored on the job and must never
    re-split.
    """
    if not (0.0 < val_fraction < 1.0):
        raise DatasetError("val_fraction must be in (0, 1)")
    rng = np.random.default_rng(seed)
    perm = rng.permutation(num_rows)
    n_val = max(1, int(round(num_rows * val_fraction)))
    n_val = min(n_val, num_rows - 1)
    val_idx = sorted(int(i) for i in perm[:n_val])
    train_idx = sorted(int(i) for i in perm[n_val:])
    return {"train": train_idx, "val": val_idx}


def verify_digest(path: str, expected_digest: str) -> None:
    actual = file_digest(path)
    if actual != expected_digest:
        raise DatasetError(
            "dataset digest mismatch: the file changed after registration "
            f"(expected {expected_digest[:12]}…, got {actual[:12]}…)"
        )
