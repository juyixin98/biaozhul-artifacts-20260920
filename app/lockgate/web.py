"""Dependency-lock consistency gate — FastAPI service."""
from __future__ import annotations

from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .archive import UnsafeArchiveError, parse_archive
from .audit import audit
from .models import AuditResponse, HealthResponse
from .platforms import Target

VERSION = "1.0.0"
MAX_UPLOAD_BYTES = 32 * 1024 * 1024

app = FastAPI(
    title="Dependency Lock Consistency Gate",
    version=VERSION,
    description=(
        "Offline audit of a Node project bundle: package.json + package-lock.json "
        "(v3), with optional registry tarballs for real digest verification. "
        "Nothing is extracted to disk or executed."
    ),
)


@app.get("/health", response_model=HealthResponse, tags=["meta"])
def health() -> HealthResponse:
    return HealthResponse(status="ok", version=VERSION)


@app.post(
    "/api/v1/audit",
    response_model=AuditResponse,
    tags=["audit"],
    summary="Audit an uploaded project archive",
)
async def audit_bundle(
    bundle: UploadFile = File(..., description=".zip or .tar(.gz) containing package.json and package-lock.json"),
    os: Optional[str] = Form(default=None, description="target OS for optional-dep evaluation, e.g. linux"),
    cpu: Optional[str] = Form(default=None, description="target CPU, e.g. x64"),
    libc: Optional[str] = Form(default=None, description="glibc or musl"),
    include_dev: bool = Form(default=True),
    package_path: Optional[str] = Form(default=None),
    lock_path: Optional[str] = Form(default=None),
    allowed_registry: Optional[str] = Form(
        default=None, description="comma-separated registry host allowlist"),
) -> AuditResponse:
    raw = await bundle.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="archive exceeds 32 MiB upload cap")
    try:
        archive = parse_archive(raw)
    except UnsafeArchiveError as exc:
        # unsafe archives are a client error, not an audit result
        raise HTTPException(status_code=422, detail=f"unsafe or invalid archive: {exc}") from exc

    target = None
    if os or cpu:
        if not (os and cpu):
            raise HTTPException(status_code=400, detail="os and cpu must be provided together")
        target = Target(os=os, cpu=cpu, libc=libc)
    elif libc:
        raise HTTPException(status_code=400, detail="libc requires os and cpu")

    registries = None
    if allowed_registry:
        registries = [h.strip() for h in allowed_registry.split(",") if h.strip()]
        if not registries:
            raise HTTPException(status_code=400, detail="empty registry allowlist")

    result = audit(
        archive,
        target=target,
        include_dev=include_dev,
        allowed_registries=registries,
        package_path=package_path,
        lock_path=lock_path,
    )
    # findings always 200: a failed gate is a successful *audit*.
    return AuditResponse(**result)


@app.exception_handler(Exception)
async def unhandled(_request, exc: Exception) -> JSONResponse:  # pragma: no cover
    return JSONResponse(status_code=500, content={"detail": f"internal error: {exc!r}"})
