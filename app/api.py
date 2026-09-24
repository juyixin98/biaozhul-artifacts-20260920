"""FastAPI application exposing the offline lock-consistency gate."""

from __future__ import annotations

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from pydantic import ValidationError

from .extract import BundleError, extract_bundle
from .models import ReportModel
from .verifier import VALID_CPU, VALID_LIBC, VALID_OS, verify_bundle

app = FastAPI(
    title="Dependency Lock Consistency Gate",
    version="1.0.0",
    description=(
        "Offline verification service for a single npm package.json plus "
        "package-lock.json (lockfileVersion 3). It cryptographically validates "
        "vendored tarballs, dependency ranges, resolution, workspaces and "
        "optional/platform packages. No install scripts are ever executed."
    ),
)


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok", "service": "lock-consistency-gate"}


def _validate_platform(os_name: str, cpu: str, libc: str | None) -> None:
    if os_name not in VALID_OS:
        raise HTTPException(
            status_code=400,
            detail=f"os must be one of {sorted(VALID_OS)}, got {os_name!r}",
        )
    if cpu not in VALID_CPU:
        raise HTTPException(
            status_code=400,
            detail=f"cpu must be one of {sorted(VALID_CPU)}, got {cpu!r}",
        )
    if libc is not None and libc not in VALID_LIBC:
        raise HTTPException(
            status_code=400,
            detail=f"libc must be one of {sorted(VALID_LIBC)} or null",
        )


def _run(files: dict[str, bytes], **kwargs: object) -> ReportModel:
    try:
        raw_report = verify_bundle(files, **kwargs)  # type: ignore[arg-type]
        return ReportModel.model_validate(raw_report)
    except ValidationError as exc:
        # Indicates a bug in report assembly, never user input shape.
        raise HTTPException(status_code=500, detail=exc.errors()) from exc


@app.post(
    "/api/v1/verify",
    response_model=ReportModel,
    tags=["verify"],
    summary="Verify an uploaded project bundle (.tgz/.tar.gz/.zip)",
)
async def verify_archive(
    file: UploadFile = File(..., description="gzip tar or zip containing package.json, package-lock.json and optional vendor/ tarballs"),
    os: str = Query("linux"),
    cpu: str = Query("x64"),
    libc: str | None = Query("glibc"),
    production: bool = Query(False, description="exclude devDependencies"),
    require_vendored_tarballs: bool = Query(
        False,
        description="treat any missing vendor/ tarball as a hard error",
    ),
) -> ReportModel:
    _validate_platform(os, cpu, libc)
    blob = await file.read()
    try:
        bundle = extract_bundle(blob)
    except BundleError as exc:
        raise HTTPException(status_code=400, detail=f"rejected bundle: {exc}") from exc
    return _run(
        bundle.files,
        os_name=os,
        cpu=cpu,
        libc=libc,
        production=production,
        require_vendored_tarballs=require_vendored_tarballs,
    )


@app.post(
    "/api/v1/verify/files",
    response_model=ReportModel,
    tags=["verify"],
    summary="Verify a bundle formed from individually uploaded files",
)
async def verify_files(
    files: list[UploadFile] = File(
        ...,
        description="all files of the project keyed by their archive-relative path "
        "(package.json, package-lock.json, vendor/.../*.tgz)",
    ),
    os: str = Query("linux"),
    cpu: str = Query("x64"),
    libc: str | None = Query("glibc"),
    production: bool = Query(False),
    require_vendored_tarballs: bool = Query(False),
) -> ReportModel:
    _validate_platform(os, cpu, libc)
    mapping: dict[str, bytes] = {}
    for upload in files:
        mapping[upload.filename or ""] = await upload.read()
    # Run the same path-safety checks as archive extraction.
    from .extract import safe_member_path, strip_common_root

    validated: dict[str, bytes] = {}
    for name, data in mapping.items():
        path = safe_member_path(name)
        if path in (".", ""):
            raise HTTPException(status_code=400, detail="file with empty name")
        validated[path] = data
    validated = strip_common_root(validated)
    if "package.json" not in validated:
        raise HTTPException(status_code=400, detail="package.json is required")
    return _run(
        validated,
        os_name=os,
        cpu=cpu,
        libc=libc,
        production=production,
        require_vendored_tarballs=require_vendored_tarballs,
    )
