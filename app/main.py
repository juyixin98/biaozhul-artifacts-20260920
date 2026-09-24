"""FastAPI 应用：制品签名与信任轮换 HTTP 接口。

启动::

    uvicorn app.main:app --reload
    STATE_PATH=data/state.json uvicorn app.main:app
"""

from __future__ import annotations

import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__, schemas, service
from .store import Store

app = FastAPI(
    title="制品签名与信任轮换服务",
    version=__version__,
    description=(
        "纯后端演示：Ed25519 签名绑定 (内容摘要, 制品类型, 版本)；"
        "信任根轮换需旧根阈值签名批准；拒绝版本回退与重复签名。"
    ),
)

# 单实例内存存储；设置环境变量 STATE_PATH 后落盘 JSON（原子写）。
_store = Store(os.environ.get("STATE_PATH"))


@app.exception_handler(service.ServiceError)
async def _service_error_handler(_request: Request, exc: service.ServiceError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/healthz", tags=["meta"])
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


# --- 信任根管理 --------------------------------------------------------------


@app.post("/root/init", response_model=schemas.RootView, tags=["root"], status_code=201)
def root_init(req: schemas.RootInitRequest) -> dict:
    return service.init_root(_store, req)


@app.post("/root/rotate", response_model=schemas.RootView, tags=["root"])
def root_rotate(req: schemas.RootRotateRequest) -> dict:
    return service.rotate_root(_store, req)


@app.get("/root", response_model=schemas.RootView, tags=["root"])
def root_get() -> dict:
    return service.get_root(_store)


# --- 制品签名 ----------------------------------------------------------------


@app.post(
    "/artifacts/sign",
    response_model=schemas.ArtifactRecord,
    tags=["artifact"],
    status_code=201,
)
def artifact_sign(req: schemas.ArtifactSignRequest) -> dict:
    """登记一份已离线签名的制品。签名应由构建方在本地完成，本服务只验签不持私钥。"""

    return service.register_artifact(_store, req)


@app.post("/artifacts/verify", response_model=schemas.VerifyResponse, tags=["artifact"])
def artifact_verify(req: schemas.VerifyRequest) -> dict:
    return service.verify_artifact(_store, req)


@app.get("/artifacts", response_model=list[schemas.ArtifactRecord], tags=["artifact"])
def artifact_list() -> list[dict]:
    return service.list_artifacts(_store)
