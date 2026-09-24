"""FastAPI 入口：归档预检与隔离解包服务（纯后端）。

接口
----
* ``GET  /health``                         健康检查
* ``POST /api/v1/archives/precheck``       只预检，不写文件
* ``POST /api/v1/archives/extract``        预检 + 隔离解包 + 原子发布
* ``GET  /api/v1/extractions/{id}``        查询已发布解包的元数据
* ``POST /api/v1/keys/ed25519/generate``   生成测试用 Ed25519 密钥对

签名（可选）：向 extract/precheck 增加 ``X-Signature`` 头时，必须同时
提供 ``X-Public-Key`` 头，服务会用 Ed25519 校验签名，签名内容为上传的
原始归档字节。
"""

from __future__ import annotations

import json
import os

from fastapi import FastAPI, File, Header, Request, UploadFile
from fastapi.responses import JSONResponse

from . import __version__
from .config import Settings
from .crypto import generate_keypair, verify_signature
from .errors import SignatureError, UnsafeArchiveError
from .safe_tar import ArchiveReadError
from . import storage

_READ_CHUNK = 1024 * 1024


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    quarantine_dir = os.path.join(settings.data_dir, "quarantine")
    extracted_root = os.path.join(settings.data_dir, "extracted")
    os.makedirs(quarantine_dir, exist_ok=True)
    os.makedirs(extracted_root, exist_ok=True)

    app = FastAPI(
        title="归档解包安全检查服务",
        version=__version__,
        description="支持 tar 子集的安全预检与隔离解包：阻断绝对路径、目录穿越、"
        "链接逃逸与解压炸弹。",
    )

    @app.exception_handler(UnsafeArchiveError)
    async def _unsafe_handler(_request: Request, exc: UnsafeArchiveError) -> JSONResponse:
        return JSONResponse(
            status_code=422 if exc.code != "upload_too_large" else 413,
            content={"ok": False, "error": {"code": exc.code, "detail": exc.detail}},
        )

    @app.exception_handler(ArchiveReadError)
    async def _read_handler(_request: Request, exc: ArchiveReadError) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": {"code": "unreadable_archive", "detail": exc.detail},
            },
        )

    @app.exception_handler(SignatureError)
    async def _sig_handler(_request: Request, exc: SignatureError) -> JSONResponse:
        return JSONResponse(
            status_code=401,
            content={"ok": False, "error": {"code": exc.code, "detail": exc.detail}},
        )

    @app.exception_handler(Exception)
    async def _unexpected_handler(_request: Request, exc: Exception) -> JSONResponse:
        # 兜底：不向调用方泄漏内部绝对路径/堆栈
        return JSONResponse(
            status_code=500,
            content={
                "ok": False,
                "error": {"code": "internal", "detail": "服务内部错误，请查看服务日志"},
            },
        )

    @app.get("/health")
    async def health() -> dict:
        return {"ok": True, "service": "archive-guard", "version": __version__}

    @app.post("/api/v1/keys/ed25519/generate")
    async def create_keypair() -> dict:
        # 仅用于联调/测试：私钥在响应中返回，生产环境请离线自行生成
        key = generate_keypair()
        return {
            "key_id": key.key_id,
            "public_key_pem": key.public_key_pem,
            "private_key_pem": key.private_key_pem,
        }

    async def _read_upload(
        archive: UploadFile,
        x_signature: str | None,
        x_public_key: str | None,
    ) -> tuple[list[bytes], str]:
        chunks: list[bytes] = []
        size = 0
        while True:
            chunk = await archive.read(_READ_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > settings.max_upload_bytes:  # 尽早在 HTTP 层拒绝
                raise UnsafeArchiveError(
                    "upload_too_large",
                    f"上传体积 {size} 超过上限 {settings.max_upload_bytes}",
                )
            chunks.append(chunk)
        if not chunks:
            raise UnsafeArchiveError("empty_upload", "上传内容为空")
        if x_signature:
            # 对“接收到的原始字节”验签
            verify_signature(x_public_key, x_signature, b"".join(chunks))
        return chunks, f"{size} bytes"

    @app.post("/api/v1/archives/precheck")
    async def precheck(
        archive: UploadFile = File(..., description="tar/tar.gz/tar.bz2/tar.xz 文件"),
        x_signature: str | None = Header(default=None),
        x_public_key: str | None = Header(default=None),
    ) -> dict:
        chunks, _ = await _read_upload(archive, x_signature, x_public_key)
        path, size, sha256 = storage.spool_upload(chunks, quarantine_dir, settings)
        try:
            report = storage.precheck_archive(path, settings)
        finally:
            storage.discard_quarantine(path)
        report["archive_sha256"] = sha256
        report["archive_size"] = size
        report["ok"] = True
        return report

    @app.post("/api/v1/archives/extract")
    async def extract(
        archive: UploadFile = File(..., description="tar/tar.gz/tar.bz2/tar.xz 文件"),
        x_signature: str | None = Header(default=None),
        x_public_key: str | None = Header(default=None),
    ) -> dict:
        chunks, _ = await _read_upload(archive, x_signature, x_public_key)
        path, size, sha256 = storage.spool_upload(chunks, quarantine_dir, settings)
        result = storage.extract_from_quarantine(
            path, size, sha256, settings, settings.data_dir
        )
        # 发布后在发布目录写一份清单（不是归档内容，是我们自己的元数据）
        manifest = result.to_manifest()
        with open(
            os.path.join(settings.data_dir, "extracted", result.extraction_id,
                         ".manifest.json"),
            "w",
            encoding="utf-8",
        ) as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return {"ok": True, **manifest}

    @app.get("/api/v1/extractions/{extraction_id}")
    async def get_extraction(extraction_id: str) -> JSONResponse:
        if len(extraction_id) != 32 or not all(c in "0123456789abcdef" for c in extraction_id):
            raise UnsafeArchiveError("bad_id", "extraction_id 必须为 32 位十六进制")
        manifest_path = os.path.join(
            extracted_root, extraction_id, ".manifest.json"
        )
        if not os.path.isfile(manifest_path):
            return JSONResponse(
                status_code=404,
                content={
                    "ok": False,
                    "error": {"code": "not_found", "detail": "解包结果不存在"},
                },
            )
        with open(manifest_path, encoding="utf-8") as fh:
            return JSONResponse(content={"ok": True, **json.load(fh)})

    app.state.settings = settings
    return app


app = create_app()
