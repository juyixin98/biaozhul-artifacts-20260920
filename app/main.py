"""FastAPI 入口：离线 SBOM 核对服务（纯后端，无前端）。"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from . import __version__, db as dbmod
from .engine import load_fixture
from .sbom import SbomError
from .service import DEFAULT_DB, DEFAULT_FIXTURE, run_check

FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "vulnerabilities.json"
DB_PATH = DEFAULT_DB


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.vulns, app.state.fixture_meta = load_fixture(FIXTURE_PATH)
    app.state.db = dbmod.connect(DB_PATH)
    try:
        yield
    finally:
        app.state.db.close()


app = FastAPI(
    title="Offline SBOM Dependency Matcher",
    version=__version__,
    description="离线 CycloneDX SBOM 核对服务：按生态真实版本规则与本地漏洞夹具比对。",
    lifespan=lifespan,
)


@app.exception_handler(SbomError)
async def sbom_error_handler(_: Request, exc: SbomError) -> JSONResponse:
    return JSONResponse(status_code=422,
                        content={"error": "unsupported_or_invalid_sbom",
                                 "detail": str(exc)})


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "version": __version__,
        "data_source": {
            "type": "local_fixture",
            "vulnerability_count": len(app.state.vulns),
            "generated_at": app.state.fixture_meta.get("generated_at"),
            "not_real_advisory": bool(
                app.state.fixture_meta.get("not_real_advisory", False)),
            "notice": "本地合成夹具，非实时漏洞情报，不覆盖最新漏洞。",
        },
        "supported": {
            "cyclonedx_spec_versions": ["1.4", "1.5", "1.6"],
            "ecosystems": ["npm", "maven", "pypi", "gem", "deb"],
        },
    }


@app.get("/vulnerabilities")
async def list_vulnerabilities() -> dict[str, Any]:
    return {
        "count": len(app.state.vulns),
        "notice": "本地合成夹具条目，均为虚构测试数据。",
        "vulnerabilities": [
            {"id": v.id, "ecosystem": v.ecosystem,
             "namespace": v.namespace, "name": v.name,
             "version_range": v.version_range, "range_type": v.range_type,
             "summary": v.summary, "fixed_versions": list(v.fixed_versions)}
            for v in app.state.vulns
        ],
    }


@app.post("/api/v1/sbom/check")
async def check_sbom(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        raise HTTPException(status_code=400, detail="request body is empty")
    try:
        import json
        doc = json.loads(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400,
                            detail=f"body is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict):
        raise HTTPException(status_code=400,
                            detail="SBOM body must be a JSON object")
    # SbomError 由 exception handler 转 422
    try:
        return run_check(doc, app.state.vulns, app.state.fixture_meta,
                         app.state.db, raw)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/v1/runs")
async def get_runs(disposition: str | None = None,
                   limit: int = 50) -> dict[str, Any]:
    if disposition not in (None, "affected", "unknown", "not_affected"):
        raise HTTPException(status_code=400,
                            detail="disposition must be one of "
                                   "affected|unknown|not_affected")
    limit = max(1, min(limit, 200))
    rows = dbmod.list_runs(app.state.db, disposition=disposition, limit=limit)
    return {"count": len(rows), "runs": rows}


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str) -> dict[str, Any]:
    row = dbmod.get_run(app.state.db, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return row
