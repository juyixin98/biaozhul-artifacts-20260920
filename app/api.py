"""FastAPI 接口层：纯后端，无界面。

端点
----
POST /api/bootstrap   带外引导，上传自签名 1.root.json
POST /api/update      提交 timestamp/snapshot/targets[/root] + 目标文件
GET  /api/state       查看当前信任状态（版本与目标清单）
GET  /api/targets/{name}  按当前可信清单下载目标（再次校验哈希）
GET  /api/metadata/{role} 下载当前已接受的某角色元数据（原始 JSON）
POST /api/reset       清空状态（演示/测试用）
GET  /api/health      健康检查
"""

from __future__ import annotations

import os
from typing import Iterable

from fastapi import FastAPI, File, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from . import __version__, metadata as md, updater
from .errors import MetadataError, NoStateError, UpdateRejected

DEFAULT_DATA_DIR = os.environ.get("UPDATER_DATA_DIR", "data")

# multipart 表单中四段元数据的字段名
_ROOT_FIELD = "root"
_ROLE_FIELDS = {
    md.TIMESTAMP: "timestamp",
    md.SNAPSHOT: "snapshot",
    md.TARGETS: "targets",
}


def create_app(data_dir: str | None = None) -> FastAPI:
    """应用工厂；测试可传入临时 data 目录。"""

    store = updater.TrustStore(data_dir or DEFAULT_DATA_DIR)
    app = FastAPI(
        title="离线软件更新元数据验证器",
        version=__version__,
        description="TUF 风格的 root/targets/snapshot/timestamp 四级签名验证，"
        "防回滚、防冻结、防混搭，信任状态原子提交。",
    )

    # 所有“拒绝更新”错误统一返回结构化 JSON，不泄露堆栈
    @app.exception_handler(UpdateRejected)
    async def _rejected_handler(_: Request, exc: UpdateRejected) -> JSONResponse:
        return JSONResponse(
            status_code=getattr(exc, "http_status", 400),
            content={
                "accepted": False,
                "error": exc.reason,
                "message": str(exc),
            },
        )

    @app.get("/api/health")
    async def health() -> dict:
        return {"status": "ok", "version": __version__}

    @app.post("/api/bootstrap")
    async def bootstrap(root: UploadFile = File(...)) -> dict:
        raw = await _read_upload(root, updater.MAX_METADATA_SIZE)
        result = store.bootstrap(raw)
        result["accepted"] = True
        return result

    @app.post("/api/update")
    async def update(
        timestamp: UploadFile = File(...),
        snapshot: UploadFile = File(...),
        targets: UploadFile = File(...),
        root: UploadFile | None = File(None),
        files: list[UploadFile] = File(default_factory=list),
    ) -> dict:
        bundle = updater.Bundle(
            timestamp=await _read_upload(timestamp, updater.MAX_METADATA_SIZE),
            snapshot=await _read_upload(snapshot, updater.MAX_METADATA_SIZE),
            targets=await _read_upload(targets, updater.MAX_METADATA_SIZE),
            root=(
                await _read_upload(root, updater.MAX_METADATA_SIZE)
                if root is not None
                else None
            ),
            files=await _read_target_files(files),
        )
        result = store.apply_update(bundle)
        result["accepted"] = True
        return result

    @app.get("/api/state")
    async def state() -> dict:
        return store.status()

    @app.get("/api/metadata/{role}")
    async def get_metadata(role: str) -> Response:
        if role not in md.ROLES:
            raise MetadataError(f"未知角色 {role}")
        current = store.load()
        if current is None:
            raise NoStateError("尚未引导")
        piece = getattr(current, role)
        if piece is None:
            raise MetadataError(f"角色 {role} 尚未提交任何元数据")
        return Response(content=piece.raw, media_type="application/json")

    @app.get("/api/targets/{name}")
    async def download_target(name: str) -> Response:
        data, meta_obj = store.open_target(name)
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={
                "Content-Length": str(meta_obj.length),
                "X-Content-SHA256": meta_obj.sha256,
            },
        )

    @app.post("/api/reset")
    async def reset() -> dict:
        store.reset()
        return {"status": "reset"}

    return app


async def _read_upload(upload: UploadFile, limit: int) -> bytes:
    data = await upload.read(limit + 1)
    if len(data) > limit:
        raise MetadataError(
            f"{upload.filename}: 超过大小上限 {limit} 字节，拒绝读取"
        )
    return data


async def _read_target_files(uploads: Iterable[UploadFile]) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for upload in uploads:
        name = upload.filename or ""
        # 只保留 basename，避免客户端路径影响服务端；最终合法性由验证器裁定
        name = os.path.basename(name)
        if not name:
            raise MetadataError("目标文件缺少文件名")
        if name in files:
            raise MetadataError(f"目标文件 {name} 重复上传")
        files[name] = await _read_upload(upload, updater.MAX_TARGET_SIZE)
    return files


# uvicorn app.api:app 直接可用
app = create_app()
