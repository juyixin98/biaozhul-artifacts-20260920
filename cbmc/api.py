"""FastAPI application exposing the bounded model checker (pure JSON API)."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .checker import CheckConfig, check_contract
from .errors import ContractError, ReplayError
from .fixtures import FIXTURES, get_fixture, list_fixtures, load_example
from .model import Contract, load_contract
from .replay import replay_trace

app = FastAPI(
    title="Contract Bounded Model Checker",
    version="1.0.0",
    description=(
        "Bounded verification of explicit JSON finite-state contracts using Z3. "
        "Checks balance non-negativity and total conservation and returns the "
        "shortest concrete counterexample. Results are strictly bounded: "
        "`safe_within_bound` is not a proof of arbitrary-depth safety."
    ),
)


class CheckRequest(BaseModel):
    contract: dict[str, Any] = Field(
        ..., description="Contract document conforming to cbmc-contract/v1."
    )
    steps: int = Field(
        5, ge=0, le=1000,
        description="Maximum number of transitions to unroll.",
    )
    timeout_ms: int = Field(
        10_000, ge=1, le=600_000,
        description="Per-call Z3 timeout in milliseconds.",
    )
    checks: list[str] = Field(
        default_factory=lambda: ["nonnegative", "conservation", "targets"],
        description="Subset of: nonnegative, conservation, targets.",
    )
    replay: bool = Field(
        default=True,
        description="Also concretely replay a found counterexample and attach "
                    "the independent replay report.",
    )


class ReplayRequest(BaseModel):
    contract: dict[str, Any]
    trace: list[dict[str, Any]]
    compare_to_solver: bool = True


def _load(raw: dict[str, Any]) -> Contract:
    try:
        return load_contract(raw)
    except ContractError as exc:
        raise HTTPException(status_code=400, detail=f"invalid contract: {exc}")


@app.exception_handler(ReplayError)
async def replay_error_handler(_request: Any, exc: ReplayError) -> JSONResponse:
    return JSONResponse(status_code=422,
                        content={"detail": f"replay failed: {exc}"})


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/fixtures")
def fixtures() -> dict[str, Any]:
    return {"fixtures": list_fixtures()}


@app.get("/api/fixtures/{fixture_id}")
def fixture_detail(fixture_id: str) -> dict[str, Any]:
    try:
        return get_fixture(fixture_id)
    except ContractError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.post("/api/check")
def api_check(req: CheckRequest) -> dict[str, Any]:
    contract = _load(req.contract)
    try:
        config = CheckConfig(
            steps=req.steps, timeout_ms=req.timeout_ms, checks=tuple(req.checks)
        ).validated()
    except ContractError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    result = check_contract(contract, config)
    payload = result.to_dict()
    if req.replay and result.trace:
        try:
            report = replay_trace(contract, result.trace)
            payload["replay"] = report.to_dict()
        except ReplayError as exc:
            # A solver trace that does not replay is an internal inconsistency,
            # not a user error: report it honestly rather than hiding it.
            payload["replay_error"] = str(exc)
    return payload


@app.post("/api/replay")
def api_replay(req: ReplayRequest) -> dict[str, Any]:
    contract = _load(req.contract)
    try:
        report = replay_trace(contract, req.trace,
                              compare_to_solver=req.compare_to_solver)
    except ReplayError as exc:
        raise HTTPException(status_code=422, detail=f"replay failed: {exc}")
    return report.to_dict()


@app.post("/api/fixtures/{fixture_id}/check")
def api_check_fixture(
    fixture_id: str,
    steps: int = Query(5, ge=0, le=1000),
    timeout_ms: int = Query(10_000, ge=1, le=600_000),
    checks: list[str] = Query(
        default_factory=lambda: ["nonnegative", "conservation", "targets"]
    ),
    replay: bool = Query(True),
) -> dict[str, Any]:
    try:
        contract = load_example(fixture_id)
        config = CheckConfig(steps=steps, timeout_ms=timeout_ms,
                             checks=tuple(checks)).validated()
    except ContractError as exc:
        status_code = 404 if fixture_id not in FIXTURES else 400
        raise HTTPException(status_code=status_code, detail=str(exc))
    result = check_contract(contract, config)
    payload = result.to_dict()
    if replay and result.trace:
        try:
            payload["replay"] = replay_trace(contract, result.trace).to_dict()
        except ReplayError as exc:
            payload["replay_error"] = str(exc)
    return payload
