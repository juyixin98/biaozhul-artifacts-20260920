"""FastAPI application: safe tar preflight and quarantined extraction."""

from __future__ import annotations

import os
import uuid

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse

from .archive_guard.errors import (
    ArchiveGuardError,
    InvalidArchiveError,
    QuotaExceededError,
    UnsafeArchiveError,
)
from .archive_guard.hashing import sha256_file
from .archive_guard.safety import parse_and_validate, safe_extract
from .config import get_settings, new_spool_file
from .schemas import EntryOut, ErrorResponse, ExtractResponse, InspectResponse

_READ_CHUNK = 64 * 1024

app = FastAPI(
    title="Archive Guard",
    version="1.0.0",
    description=(
        "Backend-only service for safe tar archive preflight inspection and "
        "quarantined extraction. Blocks absolute paths, directory traversal, "
        "link escape and decompression bombs; byte quotas count actual bytes "
        "written during extraction."
    ),
)


@app.exception_handler(ArchiveGuardError)
async def _archive_guard_error_handler(_request: Request,
                                       exc: ArchiveGuardError) -> JSONResponse:
    return JSONResponse(status_code=exc.http_status, content=exc.to_dict())


def _spool_upload(upload: UploadFile, max_bytes: int) -> tuple[str, int]:
    """Stream an upload into a spool file, enforcing the raw upload quota."""
    spool = new_spool_file()
    spool.close()
    total = 0
    try:
        with open(spool.name, "wb") as out:
            while True:
                chunk = upload.file.read(_READ_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise QuotaExceededError(
                        f"upload exceeds maximum size of {max_bytes} bytes",
                        limit=max_bytes,
                        actual=total,
                    )
                out.write(chunk)
    except BaseException:
        try:
            os.unlink(spool.name)
        except OSError:
            pass
        raise
    return spool.name, total


@app.get("/health", tags=["meta"])
async def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "store_dir": os.path.abspath(settings.store_dir),
        "limits": {
            "max_upload_bytes": settings.limits.max_upload_bytes,
            "max_entries": settings.limits.max_entries,
            "max_total_written_bytes":
                settings.limits.max_total_written_bytes,
            "max_single_file_bytes": settings.limits.max_single_file_bytes,
        },
    }


@app.post(
    "/api/v1/inspect",
    response_model=InspectResponse,
    responses={
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    tags=["archives"],
    summary="Preflight: validate an uploaded archive without extracting it",
)
async def inspect_archive(file: UploadFile = File(
    ..., description="A tar archive (optionally gzip-compressed)."
)):
    settings = get_settings()
    spool_path, upload_bytes = _spool_upload(
        file, settings.limits.max_upload_bytes
    )
    try:
        report = parse_and_validate(spool_path, settings.limits)
        digest = sha256_file(spool_path)
        return InspectResponse(
            sha256=digest,
            upload_bytes=upload_bytes,
            compressed=report.compressed,
            compressed_bytes=report.compressed_bytes,
            decompression_ratio=report.decompression_ratio,
            entry_count=report.entry_count,
            kind_counts=report.kind_counts(),
            total_declared_bytes=report.total_declared_bytes,
            safe=True,
            entries=[EntryOut(**_entry_payload(m))
                     for m in report.to_dict()["entries"]],
        )
    finally:
        try:
            os.unlink(spool_path)
        except OSError:
            pass


@app.post(
    "/api/v1/extract",
    response_model=ExtractResponse,
    responses={
        413: {"model": ErrorResponse},
        422: {"model": ErrorResponse},
    },
    tags=["archives"],
    summary="Validate then extract into an isolated staging directory, "
            "publishing atomically only on full success",
)
async def extract_archive(file: UploadFile = File(
    ..., description="A tar archive (optionally gzip-compressed)."
)):
    settings = get_settings()
    spool_path, upload_bytes = _spool_upload(
        file, settings.limits.max_upload_bytes
    )
    extract_id = "ext_" + uuid.uuid4().hex
    try:
        final_dir, manifest = safe_extract(
            spool_path, settings.store_dir, extract_id, settings.limits
        )
        digest = sha256_file(spool_path)
        return ExtractResponse(
            sha256=digest,
            upload_bytes=upload_bytes,
            extract_id=extract_id,
            path=final_dir,
            entry_count=manifest["entry_count"],
            written_bytes=manifest["written_bytes"],
            entries=[EntryOut(**e) for e in manifest["entries"]],
        )
    finally:
        try:
            os.unlink(spool_path)
        except OSError:
            pass


def _entry_payload(m: dict) -> dict:
    return {
        "name": m["name"],
        "kind": m["kind"],
        "size": m["size"],
        "mode": m.get("mode"),
        "target": m.get("target"),
    }
