"""Checkpoint persistence: atomic commit, checksumming and strict validation.

On-disk layout per run::

    <run_dir>/checkpoint.payload   pickled state dict
    <run_dir>/checkpoint.meta      commit marker: sha256, size, format version

Commit protocol (crash-safe):

1. Payload is written to ``checkpoint.payload.tmp`` and fsynced.
2. It is atomically renamed over ``checkpoint.payload``.
3. Metadata (including the payload's SHA-256) is written to
   ``checkpoint.meta.tmp`` and atomically renamed to ``checkpoint.meta``.

A reader therefore only ever sees *no checkpoint* or a *fully committed*
checkpoint; a torn write after a crash is detected by the checksum in the
meta file.

A checkpoint is rejected unless it contains ALL of: model parameters,
optimizer state, RNG state and the data cursor. A weights-only file is a
format error, not a usable checkpoint.
"""

from __future__ import annotations

import hashlib
import json
import os
import pickle
from typing import Any

import numpy as np

from .config import FORMAT_VERSION, TrainConfig
from .cursor import DataCursor
from .errors import (
    CheckpointCorruptError,
    CheckpointFormatError,
    CheckpointNotFoundError,
    MissingCheckpointFieldError,
    UnsupportedCheckpointVersionError,
)

PAYLOAD_NAME = "checkpoint.payload"
META_NAME = "checkpoint.meta"
PAYLOAD_TMP = "checkpoint.payload.tmp"
META_TMP = "checkpoint.meta.tmp"

# Pickle is restricted to this build's own protocol version so a downgraded
# interpreter cannot silently misread state.
_PICKLE_PROTOCOL = pickle.HIGHEST_PROTOCOL

_TOP_FIELDS = {
    "format_version",
    "config",
    "model",
    "optimizer",
    "rng_state",
    "cursor",
    "data_fingerprint",
    "loss_history",
}
_MODEL_FIELDS = {"W", "b"}
_OPTIMIZER_FIELDS = {"vW", "vb", "lr", "momentum"}


def _fsync_dir(path: str) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(final_path: str, tmp_path: str, data: bytes) -> None:
    with open(tmp_path, "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, final_path)


def checkpoint_paths(run_dir: str) -> tuple[str, str]:
    return os.path.join(run_dir, PAYLOAD_NAME), os.path.join(run_dir, META_NAME)


def save_checkpoint(run_dir: str, state: dict[str, Any]) -> None:
    """Validate, serialize and atomically commit a checkpoint."""
    # Validate before touching disk so a malformed state never overwrites a
    # previously committed checkpoint.
    _validate_state(state)

    payload_path, meta_path = checkpoint_paths(run_dir)
    payload_tmp = os.path.join(run_dir, PAYLOAD_TMP)
    meta_tmp = os.path.join(run_dir, META_TMP)

    blob = pickle.dumps(state, protocol=_PICKLE_PROTOCOL)
    digest = hashlib.sha256(blob).hexdigest()
    meta = {
        "sha256": digest,
        "size": len(blob),
        "format_version": int(state["format_version"]),
    }

    os.makedirs(run_dir, exist_ok=True)
    _atomic_write(payload_path, payload_tmp, blob)
    _atomic_write(meta_path, meta_tmp, (json.dumps(meta, indent=2) + "\n").encode())
    _fsync_dir(run_dir)


def committed(run_dir: str) -> bool:
    payload_path, meta_path = checkpoint_paths(run_dir)
    return os.path.exists(meta_path) and os.path.exists(payload_path)


def load_checkpoint(run_dir: str) -> dict[str, Any]:
    """Load, verify and strictly validate the committed checkpoint.

    Raises
    ------
    CheckpointNotFoundError:
        Nothing committed yet.
    CheckpointCorruptError:
        Meta/payload unreadable, truncated, or checksum mismatch.
    CheckpointFormatError / subclass:
        Bytes are intact but state is structurally invalid (includes
        weights-only checkpoints and unknown format versions).
    """
    payload_path, meta_path = checkpoint_paths(run_dir)
    if not os.path.exists(meta_path) or not os.path.exists(payload_path):
        raise CheckpointNotFoundError(f"no committed checkpoint in {run_dir}")

    try:
        with open(meta_path, "rb") as f:
            meta = json.loads(f.read().decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CheckpointCorruptError(f"unreadable checkpoint meta: {exc}") from exc

    for key in ("sha256", "size"):
        if key not in meta:
            raise CheckpointCorruptError(f"checkpoint meta missing {key!r}")

    try:
        with open(payload_path, "rb") as f:
            blob = f.read()
    except OSError as exc:
        raise CheckpointCorruptError(f"unreadable checkpoint payload: {exc}") from exc

    if len(blob) != int(meta["size"]):
        raise CheckpointCorruptError(
            f"checkpoint size mismatch: meta={meta['size']} disk={len(blob)}"
        )
    actual = hashlib.sha256(blob).hexdigest()
    if actual != meta["sha256"]:
        raise CheckpointCorruptError("checkpoint checksum mismatch")

    try:
        state = pickle.loads(blob)  # noqa: S301 - local infra, bytes are checksummed
    except Exception as exc:  # unpickling can raise many error types
        raise CheckpointCorruptError(f"undecodable checkpoint payload: {exc}") from exc

    _validate_state(state)
    return state


def _validate_state(state: Any) -> None:
    if not isinstance(state, dict):
        raise CheckpointFormatError("checkpoint payload is not a dict")

    if "format_version" not in state:
        raise MissingCheckpointFieldError("checkpoint missing 'format_version'")
    version = state["format_version"]
    if version != FORMAT_VERSION:
        raise UnsupportedCheckpointVersionError(
            f"unsupported checkpoint version {version!r}; expected {FORMAT_VERSION}"
        )

    # A weights-only checkpoint must be rejected: model params alone are
    # insufficient for exact recovery.
    missing = _TOP_FIELDS - set(state)
    if missing:
        raise MissingCheckpointFieldError(
            "checkpoint is not a complete training state "
            f"(missing: {sorted(missing)}); weights-only checkpoints are rejected"
        )

    model = state["model"]
    if not isinstance(model, dict) or _MODEL_FIELDS - set(model):
        raise CheckpointFormatError("invalid 'model' section")
    W, b = np.asarray(model["W"]), np.asarray(model["b"])
    if W.ndim != 1 or W.dtype != np.float32:
        raise CheckpointFormatError("model W must be a 1-D float32 array")
    if b.shape != () or b.dtype != np.float32:
        raise CheckpointFormatError("model b must be a scalar float32")

    opt = state["optimizer"]
    if not isinstance(opt, dict) or _OPTIMIZER_FIELDS - set(opt):
        raise CheckpointFormatError(
            "invalid 'optimizer' section: optimizer velocity state is mandatory"
        )
    vW, vb = np.asarray(opt["vW"]), np.asarray(opt["vb"])
    if vW.shape != W.shape or vW.dtype != np.float32:
        raise CheckpointFormatError("optimizer vW shape/dtype mismatch")
    if vb.shape != () or vb.dtype != np.float32:
        raise CheckpointFormatError("optimizer vb must be scalar float32")

    rng_state = state["rng_state"]
    if not isinstance(rng_state, dict) or "bit_generator" not in rng_state:
        raise CheckpointFormatError("invalid 'rng_state': full BitGenerator state required")
    try:
        probe = np.random.default_rng(1234)
        probe.bit_generator.state = rng_state
    except (ValueError, TypeError) as exc:
        raise CheckpointFormatError(f"rng_state cannot be restored: {exc}") from exc

    try:
        DataCursor.from_state(state["cursor"])
    except (TypeError, KeyError, ValueError) as exc:
        raise CheckpointFormatError(f"invalid 'cursor': {exc}") from exc

    try:
        TrainConfig.from_dict(state["config"])
    except (TypeError, KeyError, ValueError) as exc:
        raise CheckpointFormatError(f"invalid 'config': {exc}") from exc

    if not isinstance(state["data_fingerprint"], str) or len(state["data_fingerprint"]) != 64:
        raise CheckpointFormatError("invalid 'data_fingerprint'")

    history = state["loss_history"]
    if not isinstance(history, list) or any(
        not isinstance(item, dict) or {"step", "loss"} - set(item) for item in history
    ):
        raise CheckpointFormatError("invalid 'loss_history'")
