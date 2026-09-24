"""JSON-file storage with atomic writes and HMAC-signed version records.

Layout:
  DATA_DIR/jobs/<job_id>.json
  DATA_DIR/cameras/<camera_id>/<width>x<height>/v<n>.json
  DATA_DIR/cameras/<camera_id>/<width>x<height>/latest -> v<n>.json
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from . import config, crypto

_CAMERA_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
_RES_RE = re.compile(r"^(\d+)x(\d+)$")
_VERSION_FILE_RE = re.compile(r"^v(\d+)\.json$")


class StorageError(Exception):
    pass


def _validate_camera(camera_id: str) -> None:
    if not _CAMERA_RE.match(camera_id):
        raise StorageError(f"illegal camera_id: {camera_id!r}")


def _resolution_dir(camera_id: str, width: int, height: int) -> Path:
    _validate_camera(camera_id)
    return config.DATA_DIR / "cameras" / camera_id / f"{width}x{height}"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def save_job(job: dict[str, Any]) -> None:
    path = config.DATA_DIR / "jobs" / f"{job['job_id']}.json"
    _atomic_write_json(path, job)


def load_job(job_id: str) -> dict[str, Any] | None:
    if not re.match(r"^[A-Za-z0-9-]{1,64}$", job_id):
        return None
    path = config.DATA_DIR / "jobs" / f"{job_id}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _sign_version(version: dict[str, Any]) -> dict[str, Any]:
    unsigned = {k: v for k, v in version.items() if k != "signature"}
    version["signature"] = crypto.sign_payload(unsigned)
    return version


def save_version(version: dict[str, Any]) -> dict[str, Any]:
    d = _resolution_dir(version["camera_id"], version["width"], version["height"])
    d.mkdir(parents=True, exist_ok=True)
    existing = list_versions(version["camera_id"], version["width"], version["height"])
    next_number = max((v["version"] for v in existing), default=0) + 1
    version["version"] = next_number
    version["version_id"] = f"v{next_number}"
    version = _sign_version(version)
    _atomic_write_json(d / f"v{next_number}.json", version)
    latest = d / "latest"
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    os.symlink(f"v{next_number}.json", latest)
    return version


def _read_version_file(path: Path) -> dict[str, Any] | None:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    signature = raw.get("signature", "")
    unsigned = {k: v for k, v in raw.items() if k != "signature"}
    raw["signature_valid"] = crypto.verify_payload(unsigned, signature)
    return raw


def list_versions(camera_id: str, width: int, height: int) -> list[dict[str, Any]]:
    d = _resolution_dir(camera_id, width, height)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for entry in d.iterdir():
        m = _VERSION_FILE_RE.match(entry.name)
        if not m:
            continue
        raw = _read_version_file(entry)
        if raw is not None:
            out.append(raw)
    out.sort(key=lambda v: v["version"])
    return out


def get_version(camera_id: str, width: int, height: int, number: int) -> dict[str, Any] | None:
    d = _resolution_dir(camera_id, width, height)
    raw = _read_version_file(d / f"v{number}.json")
    return raw


def get_latest(camera_id: str, width: int, height: int) -> dict[str, Any] | None:
    d = _resolution_dir(camera_id, width, height)
    path = d / "latest"
    if not path.exists():
        versions = list_versions(camera_id, width, height)
        return versions[-1] if versions else None
    return _read_version_file(path)


def list_resolutions(camera_id: str) -> list[tuple[int, int]]:
    _validate_camera(camera_id)
    base = config.DATA_DIR / "cameras" / camera_id
    found: set[tuple[int, int]] = set()
    if base.is_dir():
        for entry in base.iterdir():
            m = _RES_RE.match(entry.name)
            if m and entry.is_dir():
                found.add((int(m.group(1)), int(m.group(2))))
    return sorted(found)
