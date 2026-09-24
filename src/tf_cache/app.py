"""FastAPI HTTP 服务：离线 TF 树查询。

启动：
    uvicorn tf_cache.app:app --reload
    （从 src 目录启动，或设置 PYTHONPATH=src）

所有数据均为用户通过 API 写入的合成 / 离线回放数据，不连接任何真实硬件。
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .errors import (
    DuplicateTimestampError,
    ExtrapolationNotAllowedError,
    FrameNotFoundError,
    InvalidTransformError,
    TFCycleError,
)
from .se3 import SE3Transform
from .tree import TransformTree

app = FastAPI(
    title="离线 TF 时间缓存查询服务",
    version="1.0.0",
    description=(
        "存储带时间戳的 SE3 变换（平移 + 单位四元数），平移线性插值、"
        "旋转 SLERP；支持逆变换与多边组合，拒绝成环与越界外推。"
        "仅使用合成 / 离线数据，无硬件接入。"
    ),
)

# 单进程内的全局 TF 树
tree = TransformTree()


# --------------------------------------------------------------------------
# 请求 / 响应模型
# --------------------------------------------------------------------------
class TransformSample(BaseModel):
    t: float = Field(..., description="时间戳（秒，浮点）")
    translation: list[float] = Field(..., min_length=3, max_length=3)
    quaternion: list[float] = Field(
        ...,
        min_length=4,
        max_length=4,
        description="单位四元数 [x, y, z, w]（与 SciPy 约定一致）",
    )


class TransformAdd(BaseModel):
    parent: str = Field(..., min_length=1)
    child: str = Field(..., min_length=1)
    t: float
    translation: list[float] = Field(..., min_length=3, max_length=3)
    quaternion: list[float] = Field(..., min_length=4, max_length=4)


class EdgeBatchAdd(BaseModel):
    parent: str = Field(..., min_length=1)
    child: str = Field(..., min_length=1)
    samples: list[TransformSample] = Field(..., min_length=1)


class LookupRequest(BaseModel):
    source: str = Field(..., min_length=1, description="源坐标系")
    target: str = Field(..., min_length=1, description="目标坐标系")
    t: float


class BatchLookupRequest(BaseModel):
    source: str
    target: str
    times: list[float] = Field(..., min_length=1)
    on_error: Literal["raise", "null"] = Field(
        "raise",
        description="raise: 任一时刻失败则整体 4xx；null: 失败时刻返回 null",
    )


class TransformOut(BaseModel):
    t: float
    translation: list[float]
    quaternion: list[float]
    matrix: list[list[float]]


class BatchLookupResponse(BaseModel):
    source: str
    target: str
    path: list[str]
    results: list[TransformOut | None]


class PathResponse(BaseModel):
    source: str
    target: str
    path: list[str]


class EdgeInfo(BaseModel):
    parent: str
    child: str
    samples: int
    t_min: float | None
    t_max: float | None


class TreeInfo(BaseModel):
    frames: list[str]
    edges: list[EdgeInfo]


# --------------------------------------------------------------------------
# 辅助
# --------------------------------------------------------------------------
def _to_se3(translation: list[float], quaternion: list[float]) -> SE3Transform:
    try:
        return SE3Transform(translation, quaternion)
    except InvalidTransformError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _to_out(t: float, tf: SE3Transform) -> TransformOut:
    return TransformOut(
        t=float(t),
        translation=tf.translation.tolist(),
        quaternion=tf.quaternion.tolist(),
        matrix=tf.to_matrix().tolist(),
    )


def _domain_error(exc: Exception) -> HTTPException:
    """把领域异常映射为 HTTP 错误。"""
    if isinstance(exc, TFCycleError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, FrameNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, ExtrapolationNotAllowedError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, DuplicateTimestampError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, InvalidTransformError):
        return HTTPException(status_code=422, detail=str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return HTTPException(status_code=422, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


# --------------------------------------------------------------------------
# 路由
# --------------------------------------------------------------------------
@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/tree", response_model=TreeInfo)
def get_tree() -> TreeInfo:
    edges = []
    for parent, child, bounds in tree.edges():
        key = frozenset((parent, child))
        n = len(tree._edges[key])  # noqa: SLF001 - 同包内读取
        edges.append(
            EdgeInfo(
                parent=parent,
                child=child,
                samples=n,
                t_min=bounds[0] if bounds else None,
                t_max=bounds[1] if bounds else None,
            )
        )
    return TreeInfo(frames=sorted(tree.frames), edges=edges)


@app.post("/transforms", status_code=201)
def add_transform(body: TransformAdd) -> dict:
    tf = _to_se3(body.translation, body.quaternion)
    try:
        tree.add_transform(body.parent, body.child, body.t, tf)
    except (TFCycleError, DuplicateTimestampError, ValueError, TypeError) as exc:
        raise _domain_error(exc) from exc
    return {"status": "added", "parent": body.parent, "child": body.child, "t": body.t}


@app.post("/transforms/batch", status_code=201)
def add_edge_batch(body: EdgeBatchAdd) -> dict:
    samples = [(s.t, _to_se3(s.translation, s.quaternion)) for s in body.samples]
    try:
        tree.add_transforms(body.parent, body.child, samples)
    except (
        TFCycleError,
        DuplicateTimestampError,
        ValueError,
        TypeError,
        InvalidTransformError,
    ) as exc:
        raise _domain_error(exc) from exc
    return {
        "status": "added",
        "parent": body.parent,
        "child": body.child,
        "samples": len(samples),
    }


@app.post("/lookup", response_model=TransformOut)
def lookup(body: LookupRequest) -> TransformOut:
    try:
        tf = tree.lookup_transform(body.source, body.target, body.t)
    except (
        FrameNotFoundError,
        ExtrapolationNotAllowedError,
        TFCycleError,
        InvalidTransformError,
        ValueError,
    ) as exc:
        raise _domain_error(exc) from exc
    return _to_out(body.t, tf)


@app.post("/lookup/batch", response_model=BatchLookupResponse)
def lookup_batch(body: BatchLookupRequest) -> BatchLookupResponse:
    try:
        path = tree.find_path(body.source, body.target)
    except FrameNotFoundError as exc:
        raise _domain_error(exc) from exc

    results: list[TransformOut | None] = []
    for t in body.times:
        try:
            tf = tree.lookup_transform(body.source, body.target, t)
        except (ExtrapolationNotAllowedError, FrameNotFoundError) as exc:
            if body.on_error == "raise":
                raise _domain_error(exc) from exc
            results.append(None)
        else:
            results.append(_to_out(t, tf))
    return BatchLookupResponse(
        source=body.source, target=body.target, path=path, results=results
    )


@app.get("/path/{source}/{target}", response_model=PathResponse)
def get_path(source: str, target: str) -> PathResponse:
    try:
        path = tree.find_path(source, target)
    except FrameNotFoundError as exc:
        raise _domain_error(exc) from exc
    return PathResponse(source=source, target=target, path=path)


@app.delete("/tree", status_code=204)
def clear_tree() -> None:
    tree._edges.clear()  # noqa: SLF001
    tree._edge_orientation.clear()
    tree._frames.clear()
    return None
