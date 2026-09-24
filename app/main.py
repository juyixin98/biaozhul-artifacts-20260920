"""FastAPI application: calibration-chain propagation audit.

Endpoints
----------
GET  /health                          liveness + signing key fingerprint
POST /audit                           full audit (loops + covariance)
POST /chain                           open-chain propagation only
POST /montecarlo                      MC check of first-order propagation
POST /sign                            sign an arbitrary JSON payload
POST /verify                          verify a SignedBundle
POST /calibration/ingest              fingerprint+verify a calibration bundle
GET  /docs                            OpenAPI UI (framework-provided)

Response signing: every successful audit response is countersigned with the
server's Ed25519 key (real signature, written to keys/server_dev_key.pem on
first start unless overridden with CALIB_AUDIT_KEY_PATH).
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from .audit import audit_chain, audit_loops
from .crypto import (
    envelope,
    fingerprint,
    load_private_key,
    public_key_from_hex,
    save_dev_keypair,
    sign,
    verify,
)
from .models import AuditRequest, ChainRequest, SignedBundle
from .montecarlo import run_monte_carlo

KEY_PATH = Path(os.environ.get("CALIB_AUDIT_KEY_PATH", "keys/server_dev_key.pem"))
PUB_PATH = KEY_PATH.with_suffix(".pub.pem")


def _server_key():
    if not KEY_PATH.exists():
        KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
        save_dev_keypair(KEY_PATH, PUB_PATH)
    return load_private_key(KEY_PATH)


app = FastAPI(
    title="Calibration Chain Propagation Audit",
    version="1.0.0",
    description=(
        "Sensor extrinsics chain checker: first-order 6D perturbation "
        "covariance propagation with explicit correlation policy, loop "
        "closure tests, shortest-evidence diagnostics and Monte Carlo checks."
    ),
)

_SERVER_KEY = None


def server_key():
    global _SERVER_KEY
    if _SERVER_KEY is None:
        _SERVER_KEY = _server_key()
    return _SERVER_KEY


class SignRequest(BaseModel):
    payload: dict


class MCRequest(BaseModel):
    request_id: str | None = None
    calibration_version: str
    convention: str = "right"
    correlation_policy: str
    rho: float | list[list[float]] | None = None
    cross_blocks: list = []
    edges: list
    frame_path: list[str]
    loop: bool = False
    n_samples: int = 20000
    seed: int = 7


@app.get("/health")
def health():
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    priv = server_key()
    return {
        "status": "ok",
        "service": "calibration-chain-audit",
        "signing_algorithm": "Ed25519",
        "public_key_hex": priv.public_key()
        .public_bytes(Encoding.Raw, PublicFormat.Raw)
        .hex(),
    }


@app.post("/audit")
def audit(req: AuditRequest):
    body = audit_loops(req)
    body["request_id"] = req.request_id
    return envelope(body, server_key())


@app.post("/chain")
def chain_endpoint(req: ChainRequest):
    body = audit_chain(req)
    body["request_id"] = req.request_id
    return envelope(body, server_key())


@app.post("/montecarlo")
def montecarlo_endpoint(req: MCRequest):
    # edges arrive as raw dicts; re-validate through TransformSpec
    from .models import TransformSpec

    edge_specs = [TransformSpec.model_validate(e) for e in req.edges]
    cross = []
    if req.cross_blocks:
        from .models import CrossBlockSpec

        cross = [CrossBlockSpec.model_validate(c) for c in req.cross_blocks]
    result = run_monte_carlo(
        edge_specs,
        req.frame_path,
        convention=req.convention,
        correlation_policy=req.correlation_policy,
        rho=req.rho,
        cross_blocks=cross,
        n_samples=req.n_samples,
        seed=req.seed,
        loop=req.loop,
    )
    result["calibration_version"] = req.calibration_version
    return envelope(result, server_key())


@app.post("/sign")
def sign_endpoint(req: SignRequest):
    return {
        "canonical_sha256": fingerprint(req.payload),
        "signature": sign(server_key(), req.payload),
    }


@app.post("/verify")
def verify_endpoint(bundle: SignedBundle):
    try:
        pub = public_key_from_hex(bundle.public_key)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"bad public key: {e}")
    ok = verify(pub, bundle.payload, bundle.signature)
    return {
        "valid": ok,
        "canonical_sha256": fingerprint(bundle.payload),
        "algorithm": "Ed25519",
    }


@app.post("/calibration/ingest")
def ingest(bundle: SignedBundle):
    """Record-keeping ingest: verify signature, return canonical fingerprint."""
    try:
        pub = public_key_from_hex(bundle.public_key)
    except (ValueError, TypeError) as e:
        raise HTTPException(status_code=422, detail=f"bad public key: {e}")
    fp = fingerprint(bundle.payload)
    ok = verify(pub, bundle.payload, bundle.signature)
    return {
        "accepted": ok,
        "canonical_sha256": fp,
        "signature_valid": ok,
        "declared_calibration_version": bundle.payload.get("calibration_version"),
        "message": (
            "bundle signature verified" if ok else "signature verification FAILED"
        ),
    }
