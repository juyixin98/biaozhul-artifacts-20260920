"""Quota / policy limits for preflight and extraction.

Limits are grouped in one frozen dataclass so the API layer and tests can
override individual fields easily.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Limits:
    # --- Upload level -------------------------------------------------
    #: Maximum raw upload size accepted by the service (spooled on disk).
    max_upload_bytes: int = 200 * 1024 * 1024  # 200 MiB

    # --- Per archive --------------------------------------------------
    #: Maximum number of named members (files/dirs/links) per archive.
    max_entries: int = 10_000
    #: Maximum *declared* total size of regular-file payloads. This is a
    #: cheap preflight check; the authoritative check during extraction is
    #: ``max_total_written_bytes``, which counts bytes actually written.
    max_total_size: int = 512 * 1024 * 1024  # 512 MiB
    #: Authoritative quota: bytes actually written to the staging directory.
    max_total_written_bytes: int = 512 * 1024 * 1024  # 512 MiB
    #: Maximum size of a single regular file after decompression.
    max_single_file_bytes: int = 128 * 1024 * 1024  # 128 MiB
    #: Maximum depth (path components) of any member name.
    max_path_depth: int = 128

    # --- Symlink machinery -------------------------------------------
    #: Maximum number of symlink hops followed when resolving one path.
    #: Bounded to defeat link cycles / long chains.
    max_symlink_hops: int = 40
    #: Maximum byte length of a symlink target string.
    max_symlink_target_len: int = 4096

    # --- Decompression bomb protection --------------------------------
    #: Maximum gzip-compressed input accepted (compressed bytes).
    max_compressed_bytes: int = 200 * 1024 * 1024  # 200 MiB
    #: Maximum allowed decompression ratio (uncompressed / compressed).
    max_decompression_ratio: int = 100
