"""FastAPI application: offline Solidity build-provenance service."""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse

from .crypto import canonical_json, verify_signature
from .storage import Store
from .verifier import verify_job

DB_PATH = os.environ.get("PROVENANCE_DB", os.path.join("data", "provenance.db"))
KEY_PATH = os.environ.get("PROVENANCE_KEY", os.path.join("data", "receipt_key.pem"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.store = Store(DB_PATH, KEY_PATH)
    yield


app = FastAPI(
    title="Solidity Build Provenance Service",
    version="1.0.0",
    description=(
        "Offline verification of already-generated Solidity compiler output "
        "against source packages and compilation configurations. No uploaded "
        "script is ever executed."
    ),
    lifespan=lifespan,
)


def _store() -> Store:
    return app.state.store


@app.get("/health")
def health():
    return {"status": "ok", "signing_key": _store().public_key,
            "chain_head": _store().chain_head()}


@app.post("/api/v1/verify")
async def verify(
    package: UploadFile = File(..., description="zip containing the claimed sources"),
    config: str = Form(..., description="JSON compilation configuration"),
    compiler_output: str = Form(..., description="JSON compiler output document"),
    expected_runtime_code: str | None = Form(
        None, description='JSON {fullyQualifiedName: "0xlinked runtime hex"}'),
):
    try:
        cfg = json.loads(config)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"config is not valid JSON: {exc}")
    try:
        out = json.loads(compiler_output)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, f"compiler_output is not valid JSON: {exc}")
    expected = None
    if expected_runtime_code:
        try:
            expected = json.loads(expected_runtime_code)
            if not isinstance(expected, dict):
                raise ValueError("expected_runtime_code must be an object")
        except json.JSONDecodeError as exc:
            raise HTTPException(400, f"expected_runtime_code is not valid JSON: {exc}")

    package_bytes = await package.read()
    result = verify_job(package_bytes, cfg, out, expected)
    saved = _store().save_job(
        package_bytes=package_bytes, config=cfg, compiler_output=out,
        expected_rt=expected, result=result,
    )
    status = 200 if saved["status"] == "OK" else 422
    return JSONResponse(saved, status_code=status)


@app.get("/api/v1/jobs")
def list_jobs(limit: int = 100):
    return {"jobs": _store().list_jobs(limit)}


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    job = _store().get_job(job_id)
    if not job:
        raise HTTPException(404, "unknown job id")
    return job


@app.get("/api/v1/jobs/{job_id}/findings")
def get_findings(job_id: str):
    if not _store().get_job(job_id):
        raise HTTPException(404, "unknown job id")
    return {"job_id": job_id, "findings": _store().list_findings(job_id)}


@app.get("/api/v1/jobs/{job_id}/receipt")
def get_receipt(job_id: str):
    receipt = _store().get_receipt(job_id)
    if not receipt:
        raise HTTPException(404, "unknown job id")
    message = {
        "job_id": receipt["job_id"],
        "payload_digest": receipt["payload_digest"],
        "prev_chain": receipt["prev_chain"],
        "public_key": receipt["public_key"],
    }
    receipt["signature_valid"] = verify_signature(
        receipt["public_key"], canonical_json(message),
        bytes.fromhex(receipt["signature"]),
    )
    return receipt
