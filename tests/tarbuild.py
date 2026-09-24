"""Helpers for building tar fixtures in memory."""

from __future__ import annotations

import gzip
import io
import tarfile


def make_tar(members: list[tuple], *, gz: bool = False) -> bytes:
    """Build a tar from (name, payload | None, kind, extra) tuples.

    Each member is a dict with keys:
      name, kind in {"file","dir","symlink","hardlink","device"},
      payload (bytes), target (str), mode (int), typeflag override.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tf:
        for spec in members:
            ti = tarfile.TarInfo(spec["name"])
            kind = spec.get("kind", "file")
            ti.mode = spec.get("mode", 0o644)
            if kind == "file":
                payload = spec.get("payload", b"")
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE
                ti.mode = spec.get("mode", 0o755)
                tf.addfile(ti)
            elif kind == "symlink":
                ti.type = tarfile.SYMTYPE
                ti.linkname = spec["target"]
                tf.addfile(ti)
            elif kind == "hardlink":
                ti.type = tarfile.LNKTYPE
                ti.linkname = spec["target"]
                tf.addfile(ti)
            elif kind == "device":
                ti.type = spec.get("typeflag", tarfile.CHRTYPE)
                ti.devmajor = spec.get("devmajor", 1)
                ti.devminor = spec.get("devminor", 3)
                tf.addfile(ti)
            else:
                raise ValueError(kind)
    raw = buf.getvalue()
    if gz:
        out = io.BytesIO()
        with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gzfh:
            gzfh.write(raw)
        return out.getvalue()
    return raw


def write_tmp(path, data: bytes) -> str:
    with open(path, "wb") as fh:
        fh.write(data)
    return path
