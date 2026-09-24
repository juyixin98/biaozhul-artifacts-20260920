"""Versioned master-key (KEK) store, backed by a JSON file.

All key versions are retained so objects wrapped under an old master key stay
decryptable after a rotation. ``rotate()`` only adds a new version and flips
the ``current`` pointer; existing objects are untouched until re-wrapped.
"""

from __future__ import annotations

import base64
import json
import os
import threading

from . import crypto


class KeyStore:
    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                state = json.load(fh)
            self._current = int(state["current"])
            self._keys = {
                int(v): base64.b64decode(k) for v, k in state["keys"].items()
            }
        else:
            first = crypto.generate_kek()
            self._current = 1
            self._keys = {1: first}
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            self._save()

    def _save(self) -> None:
        state = {
            "current": self._current,
            "keys": {
                str(v): base64.b64encode(k).decode("ascii")
                for v, k in sorted(self._keys.items())
            },
        }
        tmp = self._path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, self._path)

    def current_version(self) -> int:
        return self._current

    def current_key(self) -> bytes:
        return self._keys[self._current]

    def get(self, version: int) -> bytes:
        """Return the KEK for ``version``; raises KeyError if unknown."""
        return self._keys[version]

    def versions(self) -> list[int]:
        return sorted(self._keys)

    def rotate(self) -> int:
        """Generate a new master-key version and make it current.

        Old versions are kept, so previously written objects remain
        decryptable whether or not they have been re-wrapped yet.
        """
        with self._lock:
            self._current += 1
            self._keys[self._current] = crypto.generate_kek()
            self._save()
            return self._current
