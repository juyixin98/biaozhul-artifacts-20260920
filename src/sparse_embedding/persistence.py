"""Transactional, sparse persistence.

Layer 4. Checkpoints contain ONLY rows that have actually taken an optimiser
step ("touched" rows). Untouched initialised rows are never written, so a
table of millions of rows does not get serialised after a tiny batch.

Transaction boundary
--------------------
A checkpoint is written into a unique ``*.tmp`` sibling, flushed and fsynced,
then published with a single atomic ``os.replace``. A companion ``manifest.json``
is published the same way and only after the data file is durable. If any
stage raises, the temp artefacts are removed and the previous good checkpoint
remains the one on disk - callers never observe a half-written checkpoint.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import ServiceConfig, TableSpec
from .errors import PersistenceError, StateShapeError
from .state import TrainingState

DATA_FILENAME = "checkpoint.npz"
MANIFEST_FILENAME = "manifest.json"
STATE_FORMAT_VERSION = 1


@dataclass(frozen=True)
class CheckpointManifest:
    format_version: int
    num_rows: int
    dim: int
    global_step: int
    n_touched: int
    optimizer: str
    dtype: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "format_version": self.format_version,
            "num_rows": self.num_rows,
            "dim": self.dim,
            "global_step": self.global_step,
            "n_touched": self.n_touched,
            "optimizer": self.optimizer,
            "dtype": self.dtype,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "CheckpointManifest":
        return cls(
            format_version=int(raw["format_version"]),
            num_rows=int(raw["num_rows"]),
            dim=int(raw["dim"]),
            global_step=int(raw["global_step"]),
            n_touched=int(raw["n_touched"]),
            optimizer=str(raw["optimizer"]),
            dtype=str(raw["dtype"]),
        )


def _fsync_dir(path: str) -> None:
    """Make a directory entry (the rename) durable where the OS supports it."""

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CheckpointStore:
    """Owns on-disk checkpoint layout and the commit transaction."""

    def __init__(self, state_dir: str) -> None:
        self.state_dir = state_dir

    # ----------------------------------------------------------------- paths

    @property
    def data_path(self) -> str:
        return os.path.join(self.state_dir, DATA_FILENAME)

    @property
    def manifest_path(self) -> str:
        return os.path.join(self.state_dir, MANIFEST_FILENAME)

    def exists(self) -> bool:
        return os.path.exists(self.data_path) and os.path.exists(self.manifest_path)

    # ---------------------------------------------------------------- write

    def save(self, state: TrainingState, cfg: ServiceConfig) -> CheckpointManifest:
        """Atomically persist only the touched rows.

        Either the full new checkpoint is visible or the previous one is; a
        failure leaves no partial state and re-raises as
        :class:`PersistenceError`.
        """

        touched = np.array(sorted(state.touched_rows), dtype=np.int64)
        manifest = CheckpointManifest(
            format_version=STATE_FORMAT_VERSION,
            num_rows=state.spec.num_rows,
            dim=state.spec.dim,
            global_step=state.global_step,
            n_touched=int(touched.shape[0]),
            optimizer=cfg.optimizer.name.value,
            dtype=str(state.weights.dtype),
        )

        try:
            os.makedirs(self.state_dir, exist_ok=True)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            raise PersistenceError(
                f"cannot create state directory {self.state_dir}: {exc}"
            ) from exc

        tmp_data = None
        tmp_manifest = None
        try:
            # Unique temp paths in the SAME directory so the final rename is
            # atomic (same filesystem). The data path MUST end in ``.npz``:
            # ``np.savez`` appends ``.npz`` to any path lacking that suffix,
            # which would otherwise write a different file than the one we
            # publish.
            token = f"{os.getpid()}-{id(state)}-{os.urandom(4).hex()}"
            tmp_data = os.path.join(self.state_dir, f".checkpoint.{token}.npz")
            tmp_manifest = os.path.join(self.state_dir, f".manifest.{token}.json")

            if touched.size:
                np.savez(
                    tmp_data,
                    indices=touched,
                    weights=state.weights[touched],
                    momentum=state.momentum[touched],
                    row_steps=state.row_steps[touched],
                    global_step=np.int64(state.global_step),
                )
            else:
                # Nothing touched yet: still write a valid, empty checkpoint so
                # existence/transaction semantics hold, but write no dense data.
                np.savez(
                    tmp_data,
                    indices=touched,
                    weights=np.empty((0, state.spec.dim)),
                    momentum=np.empty((0, state.spec.dim)),
                    row_steps=np.empty(0, dtype=np.int64),
                    global_step=np.int64(state.global_step),
                )
            self._fsync_file(tmp_data)

            with open(tmp_manifest, "w", encoding="utf-8") as fh:
                json.dump(manifest.to_dict(), fh, indent=2, sort_keys=True)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())

            # Publish data first, then manifest; both atomic on POSIX.
            os.replace(tmp_data, self.data_path)
            tmp_data = None
            os.replace(tmp_manifest, self.manifest_path)
            tmp_manifest = None
            _fsync_dir(self.state_dir)
        except (OSError, ValueError) as exc:
            raise PersistenceError(
                f"checkpoint save failed (transaction rolled back): {exc}",
                details={"state_dir": self.state_dir},
            ) from exc
        finally:
            for tmp in (tmp_data, tmp_manifest):
                if tmp and os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

        # Data is now durable exactly at the committed touched set.
        state.mark_clean(frozenset(int(i) for i in touched.tolist()))
        return manifest

    # ----------------------------------------------------------------- read

    def load(self, cfg: ServiceConfig) -> TrainingState:
        """Load a checkpoint into a freshly initialised state.

        Only touched rows are overlaid; untouched rows come from deterministic
        initialisation. Shape/config compatibility is verified or
        :class:`StateShapeError` is raised and nothing is returned.
        """

        if not self.exists():
            raise PersistenceError(
                "no checkpoint found to load",
                details={"state_dir": self.state_dir},
            )

        try:
            with open(self.manifest_path, "r", encoding="utf-8") as fh:
                manifest = CheckpointManifest.from_dict(json.load(fh))
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            raise PersistenceError(f"cannot read manifest: {exc}") from exc

        self._verify_compatibility(manifest, cfg)

        try:
            with np.load(self.data_path) as data:
                indices = data["indices"].astype(np.int64, copy=False)
                weights = data["weights"].astype(np.float64, copy=False)
                momentum = data["momentum"].astype(np.float64, copy=False)
                row_steps = data["row_steps"].astype(np.int64, copy=False)
                saved_global_step = int(data["global_step"])
        except (OSError, KeyError, ValueError) as exc:
            raise PersistenceError(f"cannot read checkpoint data: {exc}") from exc

        if weights.shape != (indices.shape[0], cfg.table.dim):
            raise StateShapeError(
                "checkpoint weight block has wrong shape",
                details={"got": list(weights.shape), "expected_cols": cfg.table.dim},
            )
        if indices.size and (int(indices.min()) < 0 or int(indices.max()) >= cfg.table.num_rows):
            raise StateShapeError(
                "checkpoint contains an index outside the configured table",
                details={"num_rows": cfg.table.num_rows},
            )

        state = TrainingState(cfg.table, seed=cfg.seed)
        if indices.size:
            state.load_rows(
                indices,
                weights,
                momentum,
                row_steps,
                advance_global_step=saved_global_step,
            )
            # Reconstruct touched set from loaded rows so later saves stay sparse.
            state._touched_ever.update(int(i) for i in indices.tolist())  # noqa: SLF001
            state._seen_ever.update(int(i) for i in indices.tolist())  # noqa: SLF001
            state.mark_clean(frozenset(int(i) for i in indices.tolist()))
        return state

    # ------------------------------------------------------------- helpers

    @staticmethod
    def _fsync_file(path: str) -> None:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _verify_compatibility(manifest: CheckpointManifest, cfg: ServiceConfig) -> None:
        spec: TableSpec = cfg.table
        problems: list[str] = []
        if manifest.num_rows != spec.num_rows:
            problems.append(f"num_rows {manifest.num_rows} != configured {spec.num_rows}")
        if manifest.dim != spec.dim:
            problems.append(f"dim {manifest.dim} != configured {spec.dim}")
        if manifest.optimizer != cfg.optimizer.name.value:
            problems.append(
                f"optimizer {manifest.optimizer} != configured {cfg.optimizer.name.value}"
            )
        if manifest.format_version != STATE_FORMAT_VERSION:
            problems.append(
                f"format_version {manifest.format_version} unsupported "
                f"(expected {STATE_FORMAT_VERSION})"
            )
        if problems:
            raise StateShapeError(
                "checkpoint is incompatible with configuration; refusing to load",
                details={"problems": problems},
            )
