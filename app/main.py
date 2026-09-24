"""FastAPI application for offline battery SOC estimation.

Pure backend, no frontend. All responses carry a safety banner: the
estimator is an experimental synthetic model and MUST NOT be used for
real charging control.
"""
from __future__ import annotations

import os
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .estimator import Sample, estimate_soc
from .evidence import EvidenceLog, EvidenceTamperError
from .params import ParamsIntegrityError, load_params
from .schemas import (
    EstimateRequest,
    IngestRequest,
    IngestResponse,
    ParamsInfo,
    SAFETY_BANNER,
    StatusResponse,
    VerifyResponse,
)
from .session_store import SessionStore

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("SOC_DATA_DIR", REPO_ROOT / "data"))
EVIDENCE_DIR = Path(os.environ.get("SOC_EVIDENCE_DIR", REPO_ROOT / "evidence"))
EVIDENCE_KEY_PATH = EVIDENCE_DIR / "hmac.key"
EVIDENCE_LOG_PATH = EVIDENCE_DIR / "chain.jsonl"


def _load_or_create_evidence_key() -> bytes:
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    if EVIDENCE_KEY_PATH.exists():
        return EVIDENCE_KEY_PATH.read_bytes()
    key = secrets.token_bytes(32)
    EVIDENCE_KEY_PATH.write_bytes(key)
    os.chmod(EVIDENCE_KEY_PATH, 0o600)
    return key


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fail fast: refuse to serve anything if the frozen params do not verify.
    try:
        app.state.params = load_params()
    except ParamsIntegrityError as exc:
        raise RuntimeError(f"startup aborted: {exc}") from exc
    app.state.store = SessionStore(app.state.params, DATA_DIR)
    app.state.evidence = EvidenceLog(EVIDENCE_LOG_PATH, _load_or_create_evidence_key())
    yield


app = FastAPI(
    title="Battery SOC Estimation Service",
    version="1.0.0",
    description="Offline Coulomb-counting SOC with trusted-rest OCV calibration. Experimental - NOT for charge control.",
    lifespan=lifespan,
)


def _params(request):
    return request.app.state.params


@app.get("/health")
def health():
    return {"status": "ok", "service": "soc-estimator", "not_for_control": SAFETY_BANNER}


@app.get("/api/v1/params", response_model=ParamsInfo)
def get_params(request: Request):
    p = _params(request)
    return ParamsInfo(
        version=p.version,
        digest=p.digest,
        sign_convention=p.raw["sign_convention"],
        verified=True,
    )


@app.post("/api/v1/estimate")
def estimate_one_shot(req: EstimateRequest, request: Request):
    p = _params(request)
    if len(req.samples) > p.max_samples_per_ingest:
        raise HTTPException(413, f"too many samples: {len(req.samples)} > {p.max_samples_per_ingest}")
    for s in req.samples:
        if abs(s.current_a) > p.max_abs_current_a:
            raise HTTPException(422, f"|current_a|={abs(s.current_a)} exceeds {p.max_abs_current_a} A")
    samples = [Sample(s.t_s, s.current_a, s.voltage_v, s.temp_c) for s in req.samples]
    if any(samples[i].t_s < samples[i - 1].t_s for i in range(1, len(samples))):
        raise HTTPException(422, "samples must be ordered by non-decreasing t_s")
    result = estimate_soc(samples, p, req.initial_soc)
    request.app.state.evidence.append(
        "one_shot_estimate",
        "-",
        {"n_samples": len(samples), "final_soc": result.final_soc, "flags": result.flags},
    )
    return JSONResponse(
        {
            "result": result.to_dict(),
            "params_version": p.version,
            "params_digest": p.digest,
            "not_for_control": SAFETY_BANNER,
        }
    )


@app.post("/api/v1/sessions", response_model=IngestResponse, status_code=201)
def create_session(req: IngestRequest, request: Request):
    p = _params(request)
    if len(req.samples) > p.max_samples_per_ingest:
        raise HTTPException(413, f"too many samples: {len(req.samples)} > {p.max_samples_per_ingest}")
    for s in req.samples:
        if abs(s.current_a) > p.max_abs_current_a:
            raise HTTPException(422, f"|current_a|={abs(s.current_a)} exceeds {p.max_abs_current_a} A")
    store: SessionStore = request.app.state.store
    evidence: EvidenceLog = request.app.state.evidence
    sess = store.create(req.replay_window_s, req.initial_soc)
    evidence.append("session_create", sess.session_id, {"replay_window_s": sess.replay_window_s})
    samples = [Sample(s.t_s, s.current_a, s.voltage_v, s.temp_c) for s in req.samples]
    if any(samples[i].t_s < samples[i - 1].t_s for i in range(1, len(samples))):
        raise HTTPException(422, "samples must be ordered by non-decreasing t_s")
    report = store.ingest(sess.session_id, samples)
    evidence.append(
        "session_ingest",
        sess.session_id,
        {
            "accepted": report.accepted,
            "rejected": report.rejected,
            "rejections": report.rejections,
        },
    )
    return IngestResponse(
        session_id=sess.session_id,
        accepted=report.accepted,
        rejected=report.rejected,
        rejections=report.rejections,
        n_stored=len(sess.samples),
        params_version=p.version,
        params_digest=p.digest,
    )


@app.post("/api/v1/sessions/{session_id}/ingest", response_model=IngestResponse)
def ingest_more(session_id: str, req: IngestRequest, request: Request):
    p = _params(request)
    store: SessionStore = request.app.state.store
    sess = store.get(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    if len(req.samples) > p.max_samples_per_ingest:
        raise HTTPException(413, f"too many samples: {len(req.samples)} > {p.max_samples_per_ingest}")
    for s in req.samples:
        if abs(s.current_a) > p.max_abs_current_a:
            raise HTTPException(422, f"|current_a|={abs(s.current_a)} exceeds {p.max_abs_current_a} A")
    samples = [Sample(s.t_s, s.current_a, s.voltage_v, s.temp_c) for s in req.samples]
    if any(samples[i].t_s < samples[i - 1].t_s for i in range(1, len(samples))):
        raise HTTPException(422, "samples must be ordered by non-decreasing t_s")
    report = store.ingest(session_id, samples)
    request.app.state.evidence.append(
        "session_ingest",
        session_id,
        {
            "accepted": report.accepted,
            "rejected": report.rejected,
            "rejections": report.rejections,
        },
    )
    return IngestResponse(
        session_id=session_id,
        accepted=report.accepted,
        rejected=report.rejected,
        rejections=report.rejections,
        n_stored=len(sess.samples),
        params_version=p.version,
        params_digest=p.digest,
    )


@app.get("/api/v1/sessions/{session_id}", response_model=StatusResponse)
def session_status(session_id: str, request: Request):
    store: SessionStore = request.app.state.store
    sess = store.get(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    p = _params(request)
    if sess.finalized and sess.result is not None:
        result = sess.result
    else:
        result = store.estimate(session_id).to_dict()
    return StatusResponse(
        session_id=session_id,
        finalized=sess.finalized,
        n_samples=len(sess.samples),
        replay_window_s=sess.replay_window_s,
        result=result,
        params_version=p.version,
        params_digest=p.digest,
    )


@app.post("/api/v1/sessions/{session_id}/finalize", response_model=StatusResponse)
def finalize_session(session_id: str, request: Request):
    store: SessionStore = request.app.state.store
    p = _params(request)
    sess = store.get(session_id)
    if sess is None:
        raise HTTPException(404, "session not found")
    result = store.finalize(session_id)
    request.app.state.evidence.append(
        "session_finalize",
        session_id,
        {"n_samples": result.n_samples, "final_soc": result.final_soc, "flags": result.flags},
    )
    return StatusResponse(
        session_id=session_id,
        finalized=True,
        n_samples=len(sess.samples),
        replay_window_s=sess.replay_window_s,
        result=result.to_dict(),
        params_version=p.version,
        params_digest=p.digest,
    )


@app.get("/api/v1/evidence/verify", response_model=VerifyResponse)
def verify_evidence(request: Request):
    ev: EvidenceLog = request.app.state.evidence
    try:
        summary = ev.verify()
    except EvidenceTamperError as exc:
        raise HTTPException(409, f"evidence chain verification FAILED: {exc}") from exc
    return VerifyResponse(**summary)
