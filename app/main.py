"""FastAPI application: offline SBOM dependency matching.

Endpoints
---------
POST /api/v1/sboms/analyze   submit a CycloneDX JSON document (raw body)
GET  /api/v1/scans           list stored scans
GET  /api/v1/scans/{scan_id} fetch a stored result
GET  /api/v1/fixture         describe the local vulnerability fixture
GET  /healthz                liveness probe
"""
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .cyclonedx import CycloneDXError, parse_cyclonedx
from .db import get_scan, init_db, list_scans, replace_fixture, save_scan
from .fixtures import load_advisories
from .graph import build_graph
from .matcher import analyze, sha256_bytes

DB_PATH = os.environ.get("SBOM_DB_PATH", str(Path(__file__).resolve().parent.parent / "data" / "sbom.db"))
FIXTURE_PATH = os.environ.get("SBOM_FIXTURE_PATH")

_state: dict = {}

FIXTURE_WARNING = (
    "Vulnerability data comes from a small, hand-maintained local fixture. "
    "It is not a live feed and MUST NOT be treated as complete or current."
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    advisories, meta = load_advisories(FIXTURE_PATH)
    conn = init_db(DB_PATH)
    replace_fixture(conn, advisories, meta)
    _state["conn"] = conn
    _state["advisories"] = advisories
    _state["meta"] = meta
    yield
    conn.close()
    _state.clear()


app = FastAPI(
    title="Offline SBOM Dependency Matching",
    version="1.0.0",
    lifespan=lifespan,
    description="CycloneDX subset parser + per-ecosystem version-range matching against a LOCAL fixture.",
)


def _fixture_info() -> dict:
    meta = _state["meta"]
    return {
        "advisory_count": len(_state["advisories"]),
        "source": meta.get("source", "local fixture"),
        "live_feed": bool(meta.get("live_feed", False)),
        "warning": FIXTURE_WARNING,
    }


@app.exception_handler(CycloneDXError)
async def _cdx_error_handler(_request: Request, exc: CycloneDXError):
    return JSONResponse(status_code=400, content={"error": "invalid_cyclonedx", "detail": str(exc)})


@app.get("/healthz")
def healthz():
    return {"status": "ok", "fixture_advisories": len(_state.get("advisories", []))}


@app.get("/api/v1/fixture")
def fixture():
    return _fixture_info()


@app.post("/api/v1/sboms/analyze")
async def analyze_sbom(request: Request) -> JSONResponse:
    raw = await request.body()
    if not raw:
        return JSONResponse(status_code=400,
                            content={"error": "empty_body", "detail": "submit a CycloneDX JSON document"})
    digest = sha256_bytes(raw)
    try:
        doc = json.loads(raw)
    except json.JSONDecodeError as exc:
        return JSONResponse(status_code=400,
                            content={"error": "invalid_json",
                                     "detail": f"document is not valid JSON: {exc}"})
    cdx = parse_cyclonedx(doc)  # CycloneDXError -> 400 via handler
    graph = build_graph(cdx)
    component_reports, findings = analyze(cdx, graph, _state["advisories"])

    scan_id = str(uuid.uuid4())
    submitted_at = datetime.now(timezone.utc).isoformat()

    components_payload = [
        {
            "node_key": r.node_key,
            "bom_refs": r.bom_refs,
            "purl": r.purl,
            "ecosystem": r.ecosystem,
            "name": r.name,
            "namespace": r.namespace,
            "version": r.version,
            "scope": r.scope,
            "relation": r.relation,
            "merged_count": r.merged_count,
            "variant": r.variant,
        }
        for r in component_reports
    ]
    status_counts: dict[str, int] = {}
    affected = 0
    for f in findings:
        status_counts[f["status"]] = status_counts.get(f["status"], 0) + 1
        if f["status"] == "affected":
            affected += 1

    result = {
        "scan_id": scan_id,
        "submitted_at": submitted_at,
        "document_ref": cdx.document_ref,
        "spec_version": cdx.spec_version,
        "input_sha256": digest,
        "summary": {
            "components_total": len(component_reports),
            "direct": sum(1 for r in component_reports if r.relation == "direct"),
            "transitive": sum(1 for r in component_reports if r.relation == "transitive"),
            "unreferenced": sum(1 for r in component_reports if r.relation == "unreferenced"),
            "merged_duplicates": sum(r.merged_count - 1 for r in component_reports),
            "affected_findings": affected,
            "status_counts": status_counts,
            "dependency_cycles": len(graph.cycles),
        },
        "components": components_payload,
        "findings": findings,
        "dependency_cycles": graph.cycles,
        "warnings": cdx.warnings,
        "fixture": _fixture_info(),
    }
    save_scan(_state["conn"], scan_id=scan_id, submitted_at=submitted_at,
              document_ref=cdx.document_ref, spec_version=cdx.spec_version,
              input_sha256=digest, components=components_payload, result=result)
    return JSONResponse(status_code=201, content=result)


@app.get("/api/v1/scans")
def scans(limit: int = 50):
    return {"scans": list_scans(_state["conn"], limit=limit)}


@app.get("/api/v1/scans/{scan_id}")
def scan_detail(scan_id: str, response: Response):
    result = get_scan(_state["conn"], scan_id)
    if result is None:
        response.status_code = 404
        return {"error": "not_found", "detail": f"no scan with id {scan_id!r}"}
    return result
