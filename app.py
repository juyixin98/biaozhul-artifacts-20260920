"""FastAPI HTTP layer for the offline TF tree query service.

Run with:  uvicorn app:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from tf_cache import (
    ConnectivityError,
    CycleError,
    ExtrapolationError,
    LookupError_,
    SE3,
    TransformTree,
)


class TransformIn(BaseModel):
    """One timestamped transform sample: pose of `child` in `parent` frame."""

    parent: str = Field(..., examples=["world"])
    child: str = Field(..., examples=["base_link"])
    time: float = Field(..., examples=[1.0])
    translation: list[float] = Field(..., min_length=3, max_length=3)
    rotation: list[float] = Field(
        ..., min_length=4, max_length=4, description="quaternion [x, y, z, w]"
    )


class TransformOut(BaseModel):
    source: str
    target: str
    time: float
    translation: list[float]
    rotation: list[float]  # quaternion [x, y, z, w]


def create_app(tree: TransformTree | None = None) -> FastAPI:
    app = FastAPI(title="Offline TF Tree Query Service", version="0.1.0")
    app.state.tree = tree if tree is not None else TransformTree()

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/frames")
    def frames() -> dict:
        t: TransformTree = app.state.tree
        return {
            "frames": t.frames,
            "edges": [{"parent": p, "child": c} for p, c in t.edges],
        }

    @app.post("/transforms", status_code=201)
    def add_transform(sample: TransformIn) -> dict:
        try:
            se3 = SE3.from_quat_translation(sample.rotation, sample.translation)
            app.state.tree.set_transform(
                sample.parent, sample.child, sample.time, se3
            )
        except CycleError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"inserted": True}

    @app.get("/lookup", response_model=TransformOut)
    def lookup(source: str, target: str, time: float) -> TransformOut:
        try:
            se3 = app.state.tree.lookup(source, target, time)
        except ExtrapolationError as exc:
            raise HTTPException(status_code=416, detail=str(exc)) from exc
        except (ConnectivityError, LookupError_) as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return TransformOut(
            source=source,
            target=target,
            time=time,
            translation=se3.translation.tolist(),
            rotation=se3.quat_xyzw().tolist(),
        )

    return app


app = create_app()
