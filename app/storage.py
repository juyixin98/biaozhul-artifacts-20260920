"""Filesystem object store: one envelope-encrypted blob per file."""

from __future__ import annotations

import os
import uuid


class ObjectStore:
    def __init__(self, directory: str):
        self._dir = directory
        os.makedirs(directory, exist_ok=True)

    def _path(self, object_id: str) -> str:
        if not object_id or any(c in object_id for c in "/\\.."):
            # ids are uuids we issued; refuse anything path-like
            raise KeyError(object_id)
        return os.path.join(self._dir, object_id)

    def new_id(self) -> str:
        return uuid.uuid4().hex

    def put(self, object_id: str, blob: bytes) -> None:
        tmp = self._path(object_id) + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, self._path(object_id))  # atomic publish

    def get(self, object_id: str) -> bytes:
        with open(self._path(object_id), "rb") as fh:
            return fh.read()

    def exists(self, object_id: str) -> bool:
        try:
            return os.path.exists(self._path(object_id))
        except KeyError:
            return False

    def list_ids(self) -> list[str]:
        return sorted(
            name
            for name in os.listdir(self._dir)
            if not name.endswith(".tmp")
        )
