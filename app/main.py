"""FastAPI 应用: 文件块信封加密 / 解密 / 主密钥轮换 HTTP 接口。

全局单例存储 (内存)。测试通过 app.dependency_overrides 替换为隔离实例。
"""

from __future__ import annotations

import base64
from typing import Any

from fastapi import Body, Depends, FastAPI, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import crypto
from .crypto import KeyUnavailableError
from .store import KeyStore, ObjectStore, RotationInterrupted


class ObjectIn(BaseModel):
    """JSON 上传方式: base64 编码的明文 (适合 curl --data)。"""

    plaintext_b64: str = Field(..., description="base64 编码的明文")
    block_size: int | None = Field(default=None, ge=1, le=64 * 1024 * 1024)


def create_app(keys: KeyStore | None = None, objects: ObjectStore | None = None) -> FastAPI:
    app = FastAPI(
        title="信封加密数据轮换服务",
        version="1.0.0",
        description="每对象独立数据密钥、AES-256-GCM 分块 AEAD、主密钥轮换仅重包裹 DEK。",
    )

    keys = keys or KeyStore()
    objects = objects or ObjectStore(keys)

    def get_keys() -> KeyStore:
        return keys

    def get_objects() -> ObjectStore:
        return objects

    # ---------- 错误处理 ----------

    @app.exception_handler(crypto.FormatError)
    async def format_error(_: Request, exc: crypto.FormatError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "format_error", "detail": str(exc)})

    @app.exception_handler(crypto.DecryptError)
    async def decrypt_error(_: Request, exc: crypto.DecryptError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"error": "decrypt_failed", "detail": str(exc)})

    @app.exception_handler(crypto.KeyUnavailableError)
    async def key_unavailable(_: Request, exc: crypto.KeyUnavailableError) -> JSONResponse:
        return JSONResponse(status_code=409, content={"error": "key_unavailable", "detail": str(exc)})

    @app.exception_handler(RotationInterrupted)
    async def rotation_interrupted(_: Request, exc: RotationInterrupted) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={
                "error": "rotation_interrupted",
                "detail": str(exc),
                "new_key_id": exc.new_key_id,
                "rewrapped": exc.rewrapped,
                "remaining": exc.remaining,
            },
        )

    # ---------- 对象接口 ----------

    @app.put("/objects/{name}", status_code=201, tags=["objects"])
    async def put_object(
        name: str,
        request: Request,
        block_size: int | None = Query(default=None, ge=1, le=64 * 1024 * 1024),
        objs: ObjectStore = Depends(get_objects),
    ) -> dict[str, Any]:
        """加密保存对象。支持两种请求体:

        * ``Content-Type: application/octet-stream`` —— 原始字节直接作为明文;
        * ``Content-Type: application/json``        —— ``{"plaintext_b64": "..."}``。
        """
        raw = await request.body()
        ctype = (request.headers.get("content-type") or "").split(";")[0].strip()
        if ctype == "application/json":
            payload = ObjectIn.model_validate_json(raw)
            try:
                plaintext = base64.b64decode(payload.plaintext_b64, validate=True)
            except Exception as exc:
                raise crypto.FormatError("plaintext_b64 不是合法 base64") from exc
            bs = block_size or payload.block_size or crypto.DEFAULT_BLOCK_SIZE
        else:
            plaintext = raw
            bs = block_size or crypto.DEFAULT_BLOCK_SIZE
        obj = objs.put(name, plaintext, bs)
        return {
            "name": name,
            "created_at": obj.created_at,
            "updated_at": obj.updated_at,
            "metadata": objs.metadata(name),
        }

    @app.get("/objects", tags=["objects"])
    async def list_objects(objs: ObjectStore = Depends(get_objects)) -> dict[str, Any]:
        return {"objects": objs.list_objects()}

    @app.get("/objects/{name}", tags=["objects"])
    async def get_object(name: str, objs: ObjectStore = Depends(get_objects)) -> Response:
        """解密并返回对象明文 (application/octet-stream)。

        认证失败时整体返回 400, 服务在任何情况下都不会返回部分明文。
        """
        try:
            plaintext = objs.open(name)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})
        return Response(
            content=plaintext,
            media_type="application/octet-stream",
            headers={"X-Object-Name": name, "X-Plaintext-Length": str(len(plaintext))},
        )

    @app.get("/objects/{name}/metadata", tags=["objects"])
    async def object_metadata(name: str, objs: ObjectStore = Depends(get_objects)) -> dict[str, Any]:
        try:
            return objs.metadata(name)
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})

    # ---------- 故障注入端点 (仅用于安全演示 / 验收测试) ----------

    @app.post("/objects/{name}/attack/{kind}", tags=["attack-simulation"], include_in_schema=False)
    async def attack(name: str, kind: str, objs: ObjectStore = Depends(get_objects)) -> Response:
        """在已存储容器上做字节级破坏, 然后尝试解密:
        ``header`` (改 wrapped DEK 一字节) / ``truncate`` (砍掉末块 3 字节) /
        ``bitflip`` (翻转末字节)。返回解密响应 (预期 400)。
        """
        try:
            blob = bytearray(objs.get_container(name))
        except KeyError as exc:
            return JSONResponse(status_code=404, content={"error": "not_found", "detail": str(exc)})
        if kind == "header":
            # 头部固定前缀 30B 之后是 wrapped_dek: 12B nonce + 密文, 翻转密文一字节
            blob[30 + 12] ^= 0xFF
        elif kind == "truncate":
            blob = blob[:-3]
        elif kind == "bitflip":
            blob[-1] ^= 0x01
        else:
            return JSONResponse(status_code=404, content={"error": "unknown_attack", "detail": kind})
        objs.set_container(name, bytes(blob))
        # 直接走正常解密路径, 证明接口层失败时不吐明文
        try:
            plaintext = objs.open(name)
        except crypto.EnvelopeError as exc:
            status = 409 if isinstance(exc, crypto.KeyUnavailableError) else 400
            err = "key_unavailable" if status == 409 else (
                "decrypt_failed" if isinstance(exc, crypto.DecryptError) else "format_error")
            return JSONResponse(status_code=status, content={"error": err, "detail": str(exc)})
        return Response(content=plaintext, media_type="application/octet-stream")

    # ---------- 主密钥 / 轮换接口 ----------

    @app.get("/keys", tags=["keys"])
    async def list_keys(ks: KeyStore = Depends(get_keys)) -> dict[str, Any]:
        return {"keys": [k.__dict__ for k in ks.list_keys()]}

    @app.post("/keys/rotate", status_code=200, tags=["keys"])
    async def rotate(
        fail_after: int | None = Query(
            default=None,
            ge=0,
            description="故障注入: 重包裹 N 个对象后模拟崩溃, 系统进入混合包裹状态",
        ),
        ks: KeyStore = Depends(get_keys),
        objs: ObjectStore = Depends(get_objects),
    ) -> dict[str, Any]:
        """生成新主密钥版本, 并把全部旧对象的 DEK 重包裹到新版本。

        数据密文块不变; 只有头部的 wrapped DEK / key_id 更新。
        """
        return objs.rotate_master(fail_after=fail_after)

    @app.post("/keys/rewrap", status_code=200, tags=["keys"])
    async def rewrap(objs: ObjectStore = Depends(get_objects)) -> dict[str, Any]:
        """幂等收尾: 把任何未使用当前主密钥的对象全部重包裹 (中断后续跑用)。"""
        return objs.rewrap_all()

    @app.delete("/keys/{key_id}", status_code=200, tags=["keys"])
    async def discard_key(key_id: str, ks: KeyStore = Depends(get_keys)) -> dict[str, Any]:
        """删除某版旧主密钥 (演示错误密钥场景: 旧对象随即无法解密)。当前密钥不可删。"""
        try:
            ks.discard(key_id)
        except KeyUnavailableError:
            raise
        except ValueError as exc:
            return JSONResponse(status_code=400, content={"error": "invalid_request", "detail": str(exc)})
        return {"deleted": key_id}

    return app


app = create_app()
