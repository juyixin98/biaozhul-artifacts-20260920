"""Deterministic stratified cross-fitting folds.

Fold assignment is generated from an explicit NumPy ``SeedSequence``/Generator
derived from the declared seed, independently shuffled within the treated and
untreated groups, then interleaved by original index. Therefore:

* assignment is reproducible from (seed, n_splits, A);
* each fold is stratified — the treated share is (nearly) constant across folds;
* the shuffle never sorts by X or Y, so folds cannot leak outcome information.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from .errors import CrossfitError
from .validation import validate_fold_sizes


@dataclass(frozen=True)
class Fold:
    fold: int
    train_idx: np.ndarray
    eval_idx: np.ndarray


def make_stratified_folds(
    a: np.ndarray, n_splits: int, seed: int, stratify: bool = True
) -> list[Fold]:
    """Return K folds with disjoint evaluation index sets covering all units."""
    n = len(a)
    validate_fold_sizes(a, n_splits)
    rng = np.random.default_rng(seed)

    assignment = np.empty(n, dtype=np.int64)
    groups = [np.where(a == 1)[0], np.where(a == 0)[0]] if stratify else [np.arange(n)]
    for group_idx in groups:
        shuffled = rng.permutation(group_idx)
        # Deal units round-robin into folds for the most balanced class counts.
        assignment[shuffled] = np.arange(len(shuffled)) % n_splits

    folds: list[Fold] = []
    for k in range(n_splits):
        eval_idx = np.where(assignment == k)[0]
        train_idx = np.where(assignment != k)[0]
        if len(eval_idx) == 0 or len(train_idx) == 0:
            raise CrossfitError(
                "Empty fold produced", details={"fold": k, "n_splits": n_splits}
            )
        if np.any(np.isin(eval_idx, train_idx, assume_unique=False)):
            raise CrossfitError("Fold leakage: train/eval overlap", details={"fold": k})
        folds.append(Fold(fold=k, train_idx=np.sort(train_idx), eval_idx=np.sort(eval_idx)))

    covered = np.concatenate([f.eval_idx for f in folds])
    if len(np.unique(covered)) != n:
        raise CrossfitError("Cross-fitting folds do not partition the data exactly once")
    return folds
