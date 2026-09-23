"""Checkpoint persistence and restoration.

A checkpoint captures everything required to resume a replay deterministically
after the service restarts:

* the bag summary (uri, storage, time range, topics and per-topic counts)
* the **source digest**: a map of every bag file to its SHA-256 hash. On
  restore the files on disk are rehashed and compared; any addition, removal or
  modification rejects the checkpoint — replay position is meaningless against
  a changed bag.
* the active topic filter
* the precise message position (canonical sequence of the next message plus
  its timestamp)
* rate, play state and the replay generation at save time

Files are written atomically (temp file + ``os.replace``) and fsynced, so a
crash mid-save never leaves a torn checkpoint.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .bagio import BagError, BagIndex, files_match

CHECKPOINT_VERSION = 1


class CheckpointError(Exception):
    pass


@dataclass
class Checkpoint:
    checkpoint_id: str
    created_ns: int
    version: int
    bag: dict[str, Any]
    topics: list[str] | None
    position: dict[str, int | None]
    rate: float
    state: str
    generation: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "checkpoint_id": self.checkpoint_id,
            "created_ns": self.created_ns,
            "version": self.version,
            "bag": self.bag,
            "topics": self.topics,
            "position": self.position,
            "rate": self.rate,
            "state": self.state,
            "generation": self.generation,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Checkpoint":
        try:
            return cls(
                checkpoint_id=str(data["checkpoint_id"]),
                created_ns=int(data["created_ns"]),
                version=int(data.get("version", 1)),
                bag=dict(data["bag"]),
                topics=(list(data["topics"]) if data.get("topics") is not None else None),
                position=dict(data["position"]),
                rate=float(data.get("rate", 1.0)),
                state=str(data.get("state", "paused")),
                generation=int(data.get("generation", 0)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointError(f"malformed checkpoint: {exc}") from exc


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=".tmp-", suffix=".json", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
        # fsync the directory entry so the rename itself is durable.
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:  # pragma: no cover - platform dependent
            pass
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class CheckpointStore:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, checkpoint_id: str) -> Path:
        if not checkpoint_id or "/" in checkpoint_id or checkpoint_id in {".", ".."}:
            raise CheckpointError(f"invalid checkpoint id: {checkpoint_id!r}")
        return self.base_dir / f"{checkpoint_id}.json"

    def save(
        self,
        checkpoint_id: str,
        index: BagIndex,
        topics: frozenset[str] | None,
        next_seq: int | None,
        next_timestamp_ns: int | None,
        rate: float,
        state: str,
        generation: int,
    ) -> Checkpoint:
        cp = Checkpoint(
            checkpoint_id=checkpoint_id,
            created_ns=time.time_ns(),
            version=CHECKPOINT_VERSION,
            bag=index.summary(),
            topics=sorted(topics) if topics else None,
            position={
                "next_seq": next_seq,
                "next_timestamp_ns": next_timestamp_ns,
            },
            rate=float(rate),
            state=state,
            generation=int(generation),
        )
        _atomic_write_json(self._path(checkpoint_id), cp.to_dict())
        return cp

    def load(self, checkpoint_id: str) -> Checkpoint:
        path = self._path(checkpoint_id)
        if not path.exists():
            raise CheckpointError(f"checkpoint not found: {checkpoint_id}")
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckpointError(f"unreadable checkpoint: {exc}") from exc
        return Checkpoint.from_dict(data)

    def delete(self, checkpoint_id: str) -> None:
        try:
            self._path(checkpoint_id).unlink()
        except FileNotFoundError:
            raise CheckpointError(f"checkpoint not found: {checkpoint_id}")

    def list_all(self) -> list[dict[str, Any]]:
        out = []
        for path in sorted(self.base_dir.glob("*.json")):
            try:
                out.append(self.load(path.stem).to_dict())
            except CheckpointError:
                continue
        return out

    # ------------------------------------------------------------- validation
    def verify_source(self, cp: Checkpoint, bag_dir: Path) -> tuple[bool, str]:
        """Rehash the bag on disk and compare to the checkpoint digest."""
        stored = cp.bag.get("files")
        if not isinstance(stored, dict) or not stored:
            return False, "checkpoint has no source digest"
        try:
            ok, reason = files_match(stored, bag_dir)
        except BagError as exc:
            return False, str(exc)
        return ok, reason
