"""隔离区落盘与解包编排（HTTP 无关）。"""

from __future__ import annotations

import hashlib
import os
import shutil
import tarfile
import tempfile
import uuid

from .config import Settings
from .errors import UnsafeArchiveError
from .safe_tar import ArchiveReadError, ExtractResult, safe_extract, plan_archive


def detect_compression(path: str) -> str:
    """按文件头魔数识别压缩方式；无法识别时抛 ArchiveReadError。"""
    with open(path, "rb") as fh:
        magic = fh.read(512)  # tar 的 ustar 魔数在头部偏移 257
    if not magic:
        raise ArchiveReadError("归档为空文件")
    if magic[:2] == b"\x1f\x8b":
        return "gzip"
    if magic[:3] == b"BZh":
        return "bzip2"
    if magic[:6] == b"\xfd7zXZ\x00":
        return "xz"
    if magic[257:262] == b"ustar":
        return "none"
    raise ArchiveReadError("无法识别的归档格式（仅支持 tar/gzip/bzip2/xz）")


def spool_upload(
    chunks: list[bytes], quarantine_dir: str, settings: Settings
) -> tuple[str, int, str]:
    """把上传字节写入隔离区临时文件，校验上传体积，返回 (路径, 字节数, sha256)。"""
    os.makedirs(quarantine_dir, exist_ok=True)
    fd, path = tempfile.mkstemp(prefix="incoming-", suffix=".arch", dir=quarantine_dir)
    digest = hashlib.sha256()
    total = 0
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in chunks:
                total += len(chunk)
                if total > settings.max_upload_bytes:
                    raise UnsafeArchiveError(
                        "upload_too_large",
                        f"上传体积 {total} 超过上限 {settings.max_upload_bytes}",
                    )
                digest.update(chunk)
                out.write(chunk)
    except BaseException:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    return path, total, digest.hexdigest()


def extract_from_quarantine(
    quarantine_path: str,
    archive_size: int,
    archive_sha256: str,
    settings: Settings,
    data_dir: str,
) -> ExtractResult:
    """探测格式 -> 调用安全解包 -> 无论成败清理隔离文件。"""
    compression = detect_compression(quarantine_path)
    extraction_id = uuid.uuid4().hex
    extract_root = os.path.join(data_dir, "extracted")
    try:
        with open(quarantine_path, "rb") as fh:
            return safe_extract(
                fh,
                extract_root,
                settings,
                extraction_id=extraction_id,
                archive_sha256=archive_sha256,
                archive_size=archive_size,
                compression=compression,
            )
    except UnsafeArchiveError:
        raise
    except ArchiveReadError:
        raise
    except tarfile.TarError as exc:
        raise ArchiveReadError(f"解包过程中归档读取失败: {exc}") from exc
    finally:
        try:
            os.unlink(quarantine_path)
        except OSError:
            pass


def discard_quarantine(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def precheck_archive(quarantine_path: str, settings: Settings) -> dict:
    """只做预检：校验全部条目与配额，不写出任何数据。"""
    compression = detect_compression(quarantine_path)
    archive_size = os.path.getsize(quarantine_path)
    try:
        with open(quarantine_path, "rb") as fh, tarfile.open(fileobj=fh, mode="r:*") as tar:
            planned, _vfs, declared_total = plan_archive(tar, settings)
    except tarfile.TarError as exc:
        raise ArchiveReadError(f"归档内容损坏，读取条目失败: {exc}") from exc

    ratio = (declared_total / archive_size) if archive_size else 0.0
    if ratio > settings.max_compression_ratio:
        raise UnsafeArchiveError(
            "compression_bomb",
            f"压缩比 {ratio:.1f} 超过上限 {settings.max_compression_ratio:g}"
            "（疑似解压炸弹）",
        )
    return {
        "accepted": True,
        "compression": compression,
        "archive_size": archive_size,
        "declared_uncompressed": declared_total,
        "compression_ratio": round(ratio, 3),
        "entry_count": len(planned),
        "entries": [
            {
                "name": name,
                "kind": item.kind,
                "size": item.size,
                "target": item.target,
            }
            for name, item in sorted(planned.items())
        ],
    }


def reset_data_dir(data_dir: str) -> None:
    """主要供测试使用：清空运行时目录。"""
    shutil.rmtree(data_dir, ignore_errors=True)
