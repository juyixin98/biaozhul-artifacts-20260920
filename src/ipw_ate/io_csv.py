"""CSV boundary loading with explicit validation.

Expected columns: ``t`` (0/1), ``y`` (numeric), then one or more covariate
columns. Column order is explicit; a header row is required.
"""

from __future__ import annotations

import csv

import numpy as np

from .errors import DataValidationError

TREATMENT_COL = "t"
OUTCOME_COL = "y"
REQUIRED = (TREATMENT_COL, OUTCOME_COL)


def load_csv(path: str) -> tuple[np.ndarray, np.ndarray, np.ndarray, tuple[str, ...]]:
    """Return ``(treatment, outcome, covariates, feature_names)`` from CSV."""
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        try:
            header = next(reader)
        except StopIteration as exc:
            raise DataValidationError(f"{path}: empty file (missing header)") from exc
        header = [h.strip() for h in header]
        for col in REQUIRED:
            if col not in header:
                raise DataValidationError(f"{path}: missing required column {col!r}")

        t_idx, y_idx = header.index(TREATMENT_COL), header.index(OUTCOME_COL)
        x_idx = [i for i in range(len(header)) if i not in (t_idx, y_idx)]
        if not x_idx:
            raise DataValidationError(f"{path}: at least one covariate column needed")
        names = tuple(header[i] for i in x_idx)

        rows: list[list[float]] = []
        for line, raw in enumerate(reader, start=2):
            if not raw or all(c.strip() == "" for c in raw):
                continue  # tolerate trailing blank lines
            if len(raw) != len(header):
                raise DataValidationError(
                    f"{path}:{line}: expected {len(header)} fields, got {len(raw)}"
                )
            try:
                rows.append([float(c) for c in raw])
            except ValueError as exc:
                raise DataValidationError(
                    f"{path}:{line}: non-numeric value"
                ) from exc

    if not rows:
        raise DataValidationError(f"{path}: header but no data rows")
    arr = np.asarray(rows, dtype=float)
    treatment = arr[:, t_idx]
    outcome = arr[:, y_idx]
    covariates = arr[:, x_idx]
    return treatment, outcome, covariates, names
