"""FastAPI service exposing the storage-layout checker over HTTP."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from backend.artifacts import ArtifactNotFound, get_base_contracts, get_layout, load_artifacts
from backend.layout_checker import check_layouts

app = FastAPI(title="Upgradeable Storage Layout Checker", version="1.0.0")


class CheckRequest(BaseModel):
    old_layout: dict[str, Any] = Field(description="compiler storageLayout of the old version")
    new_layout: dict[str, Any] = Field(description="compiler storageLayout of the new version")
    old_name: str | None = Field(default=None, description="root contract name of old version")
    new_name: str | None = Field(default=None, description="root contract name of new version")
    old_bases: list[str] | None = Field(
        default=None, description="ancestor contract names of old version, linearized order"
    )
    new_bases: list[str] | None = Field(
        default=None, description="ancestor contract names of new version, linearized order"
    )


class ArtifactCheckRequest(BaseModel):
    old: str = Field(description="contract name of the old version, e.g. BoxV1")
    new: str = Field(description="contract name of the new version, e.g. BoxV2")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/layouts")
def list_layouts() -> dict:
    artifacts = load_artifacts()
    return {"contracts": sorted(artifacts)}


@app.get("/layouts/{name}")
def layout_by_name(name: str) -> dict:
    try:
        return get_layout(name)
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/check")
def check(req: CheckRequest) -> dict:
    """Check two raw compiler storage-layout JSON objects."""
    report = check_layouts(
        req.old_layout, req.new_layout, req.old_name, req.new_name,
        req.old_bases, req.new_bases,
    )
    return report.to_dict()


@app.post("/check-artifacts")
def check_artifacts(req: ArtifactCheckRequest) -> dict:
    """Check two contracts compiled by `forge build` (looked up in out/)."""
    try:
        old_layout = get_layout(req.old)
        new_layout = get_layout(req.new)
    except ArtifactNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    report = check_layouts(
        old_layout, new_layout, req.old, req.new,
        get_base_contracts(req.old), get_base_contracts(req.new),
    )
    return {"old": req.old, "new": req.new, **report.to_dict()}
