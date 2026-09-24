"""运行配置，全部来自环境变量，带安全默认值。"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    value = int(raw)
    if value <= 0:
        raise ValueError(f"环境变量 {name} 必须为正整数，实际为 {raw!r}")
    return value


@dataclass(frozen=True)
class Settings:
    # 上传阶段：单个请求体允许的最大归档字节数（线/压缩后大小）
    max_upload_bytes: int
    # 解包阶段：允许写出的最大总字节数（配额，按实际写出字节计入）
    max_total_bytes: int
    # 单个常规文件的最大字节数
    max_file_bytes: int
    # 归档内条目数量上限
    max_entries: int
    # 符号链接链最大解析跳数（同时用于环路检测）
    max_symlink_hops: int
    # 压缩比（未压缩声明总大小 / 归档实际大小）上限
    max_compression_ratio: float
    # 服务根目录（隔离区与发布目录的父目录）
    data_dir: str

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            max_upload_bytes=_env_int("ARCH_MAX_UPLOAD_BYTES", 20 * 1024 * 1024),
            max_total_bytes=_env_int("ARCH_MAX_TOTAL_BYTES", 100 * 1024 * 1024),
            max_file_bytes=_env_int("ARCH_MAX_FILE_BYTES", 50 * 1024 * 1024),
            max_entries=_env_int("ARCH_MAX_ENTRIES", 20_000),
            max_symlink_hops=_env_int("ARCH_MAX_SYMLINK_HOPS", 16),
            max_compression_ratio=float(
                os.environ.get("ARCH_MAX_COMPRESSION_RATIO", "100")
            ),
            data_dir=os.environ.get("ARCH_DATA_DIR", os.path.join(os.getcwd(), "data")),
        )
