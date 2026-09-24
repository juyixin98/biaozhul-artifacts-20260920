"""标定版本持久化：不可变 JSON 记录，原子写入；按相机 + 分辨率组织。

目录布局：
  storage_dir/
    versions/<version_id>.json          # 不可变版本记录
    index.json                          # camera_id -> resolution(WxH) -> [version_id...]
    images/<version_id>/<image_id>.png  # 输入样本留档
    .hmac_secret                        # 自动生成的签名密钥（0600）
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .calibration import CalibrationError
from .config import settings


class VersionStore:
    def __init__(self, base_dir: str | None = None) -> None:
        self.base = Path(base_dir or settings.storage_dir)
        self.versions_dir = self.base / "versions"
        self.index_path = self.base / "index.json"
        self.versions_dir.mkdir(parents=True, exist_ok=True)
        if not self.index_path.exists():
            self._write_json(self.index_path, {})

    # ---------- 低层 ---------- #
    @staticmethod
    def _write_json(path: Path, obj: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(obj, fh, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_name, path)
        except BaseException:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
            raise

    @staticmethod
    def _read_json(path: Path) -> dict:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)

    # ---------- 版本 ---------- #
    def save_version(self, record: dict) -> None:
        """新版本一旦写入即不可变：相同 version_id 再次写入直接拒绝。"""
        target = self.versions_dir / f"{record['version_id']}.json"
        if target.exists():
            raise CalibrationError(
                "VERSION_EXISTS", f"版本 {record['version_id']} 已存在且不可变"
            )
        self._write_json(target, record)
        os.chmod(target, 0o600)

        index = self._read_json(self.index_path)
        camera = index.setdefault(record["camera_id"], {})
        w, h = record["resolution"]
        key = f"{w}x{h}"
        camera.setdefault(key, []).append(record["version_id"])
        self._write_json(self.index_path, index)

    def get_version(self, version_id: str) -> dict:
        target = self.versions_dir / f"{version_id}.json"
        if not target.exists():
            raise CalibrationError("VERSION_NOT_FOUND", f"标定版本 {version_id} 不存在")
        return self._read_json(target)

    def has_version(self, version_id: str) -> bool:
        return (self.versions_dir / f"{version_id}.json").exists()

    def list_versions(self, camera_id: str, width: int | None = None,
                      height: int | None = None) -> list[dict]:
        index = self._read_json(self.index_path)
        camera = index.get(camera_id, {})
        keys = [f"{width}x{height}"] if width and height else sorted(camera.keys())
        summaries: list[dict] = []
        for key in keys:
            for vid in camera.get(key, []):
                rec = self.get_version(vid)  # 版本不可变，直接读全文取摘要字段
                summaries.append(self._summary(rec))
        summaries.sort(key=lambda r: r["created_at"])
        return summaries

    def latest_version(self, camera_id: str, width: int, height: int) -> dict:
        versions = self.list_versions(camera_id, width, height)
        if not versions:
            raise CalibrationError(
                "VERSION_NOT_FOUND",
                f"相机 {camera_id} 在分辨率 {width}x{height} 下没有标定版本；"
                "标定版本与分辨率严格绑定，不能跨尺寸复用",
            )
        return self.get_version(versions[-1]["version_id"])

    @staticmethod
    def _summary(rec: dict) -> dict:
        return {
            "version_id": rec["version_id"],
            "camera_id": rec["camera_id"],
            "resolution": tuple(rec["resolution"]),
            "board": rec["board"],
            "created_at": rec["created_at"],
            "image_count": rec["image_count"],
            "accepted_count": rec["accepted_count"],
            "rejected_count": rec["rejected_count"],
            "overall_rms_px": rec["overall_rms_px"],
            "state": rec.get("state", "active"),
        }
