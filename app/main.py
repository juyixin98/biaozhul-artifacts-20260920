"""FastAPI 入口：仅提供后端 HTTP 接口，无界面。

接口一览
========

``GET  /health``
    健康检查与当前信任状态摘要。

``POST /updates``
    提交一整组签名元数据 + 目标文件。请求为 ``multipart/form-data``：

    * 四个角色的元数据以文件字段上传：``root``（首次必填）、``timestamp``、
      ``snapshot``、``targets``，内容为 JSON 文件；
    * 每个目标文件以 ``files`` 字段（同名可多个）上传，附带查询参数
      ``?name=app.bin`` 或表单字段 ``file_name=app.bin``；或者把所有
      目标文件以目标名作为字段名直接上传（任意自定义字段，会被自动识别）。

    验证失败返回 4xx/409/422 与机器可读错误码，且不改动本地状态；
    验证通过则原子提交信任状态并保存内容寻址的目标文件。

``GET  /targets/{name}``
    从本地已验证存储中下载目标文件（路径参数可含斜杠）。

``GET  /metadata/{role}``
    读取本地已信任的角色元数据原文（root/timestamp/snapshot/targets）。

``POST /admin/reset``
    清空信任状态（受 ``ADMIN_TOKEN`` 保护；未设置 token 时拒绝调用）。
"""

from __future__ import annotations

import asyncio
import base64
import json

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response
from fastapi import HTTPException

from .config import Settings
from .errors import VerifierError
from .storage import TrustStore, b64d
from .verifier import REQUIRED_ROLES, UpdateInput, apply_update

ROLE_FIELDS = ("root", "timestamp", "snapshot", "targets")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = TrustStore(settings.data_dir)
    store.ensure_dirs()
    update_lock = asyncio.Lock()

    app = FastAPI(
        title="离线软件更新元数据验证器",
        description="TUF 风格四角色签名链验证：root/targets/snapshot/timestamp。",
        version="1.0.0",
    )

    # ---- 异常 -> HTTP ---------------------------------------------------

    @app.exception_handler(VerifierError)
    async def _verifier_error_handler(_request: Request, exc: VerifierError):
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    # ---- 健康检查 / 状态 ------------------------------------------------

    @app.get("/health")
    async def health():
        state = store.load()
        return {
            "status": "ok",
            "trusted": {
                role: (None if state.get(role) is None
                       else {"version": state[role]["version"]})
                for role in REQUIRED_ROLES
            },
            "installed_targets": sorted(state.get("target_files", {}).keys()),
        }

    # ---- 元数据读取 -----------------------------------------------------

    @app.get("/metadata/{role}")
    async def get_metadata(role: str):
        if role not in REQUIRED_ROLES:
            raise HTTPException(status_code=404, detail=f"未知角色 {role}")
        state = store.load()
        entry = state.get(role)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"本地尚无 {role} 元数据")
        return Response(content=b64d(entry["raw_b64"]), media_type="application/json")

    # ---- 目标下载 -------------------------------------------------------

    @app.get("/targets/{name:path}")
    async def download_target(name: str):
        state = store.load()
        info = state.get("target_files", {}).get(name)
        if info is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "NOT_FOUND", "message": f"未安装目标 {name}"}},
            )
        data = store.read_blob(info["sha256"])
        if data is None:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "BLOB_MISSING",
                                   "message": f"目标 {name} 的内容文件缺失"}},
            )
        return Response(
            content=data,
            media_type="application/octet-stream",
            headers={"X-Content-Sha256": info["sha256"],
                     "X-Content-Sha512": info["sha512"]},
        )

    # ---- 更新提交 -------------------------------------------------------

    @app.post("/updates")
    async def submit_update(
        request: Request,
        root: UploadFile | None = File(default=None),
        timestamp: UploadFile | None = File(default=None),
        snapshot: UploadFile | None = File(default=None),
        targets: UploadFile | None = File(default=None),
        files: list[UploadFile] | None = File(default=None),
        manifest: str | None = Form(default=None),
    ):
        content_type = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type:
            return JSONResponse(
                status_code=415,
                content={"error": {"code": "UNSUPPORTED_MEDIA_TYPE",
                                   "message": "请使用 multipart/form-data 提交"}},
            )

        role_data: dict[str, bytes] = {}
        for role, upload in zip(ROLE_FIELDS, (root, timestamp, snapshot, targets)):
            if upload is not None:
                role_data[role] = await upload.read()

        target_files: dict[str, bytes] = {}

        # files=<file> & name=<...> 形式（OpenAPI/Swagger UI 友好）
        if files:
            names_from_query = request.query_params.getlist("name")
            for idx, upload in enumerate(files):
                name = (names_from_query[idx] if idx < len(names_from_query)
                        else upload.filename)
                if not name:
                    return JSONResponse(
                        status_code=422,
                        content={"error": {"code": "BAD_FILENAME",
                                           "message": f"第 {idx+1} 个目标文件缺少名称"}},
                    )
                target_files[name] = await upload.read()

        # 目标名作为自定义字段名直接上传（curl -F 'pkg/app.bin=@...'）
        form = await request.form()
        reserved = set(ROLE_FIELDS) | {"files", "manifest"}
        for key, value in form.multi_items():
            if key in reserved:
                continue
            if isinstance(value, str):
                continue
            if key in target_files:
                continue
            target_files[key] = await value.read()

        # manifest 可携带额外的“按名上传”内容（base64 JSON），便于无文件系统的客户端
        if manifest:
            try:
                decoded = json.loads(manifest)
                assert isinstance(decoded, dict)
                for fname, b64text in decoded.items():
                    target_files.setdefault(fname, base64.b64decode(b64text, validate=True))
            except (ValueError, AssertionError) as exc:
                return JSONResponse(
                    status_code=422,
                    content={"error": {"code": "BAD_MANIFEST",
                                       "message": f"manifest 解析失败: {exc}"}},
                )

        missing = [r for r in ("timestamp", "snapshot", "targets") if r not in role_data]
        state = store.load()
        if state.get("root") is None and "root" not in role_data:
            missing.insert(0, "root")
        if missing:
            return JSONResponse(
                status_code=422,
                content={"error": {"code": "MISSING_ROLE",
                                   "message": "缺少必需的元数据字段: " + ", ".join(missing),
                                   "detail": {"missing": missing}}},
            )

        update = UpdateInput(
            timestamp=role_data["timestamp"],
            snapshot=role_data["snapshot"],
            targets=role_data["targets"],
            files=target_files,
            root=role_data.get("root"),
        )

        loop = asyncio.get_running_loop()
        async with update_lock:
            summary = await loop.run_in_executor(
                None, lambda: apply_update(
                    store, update,
                    bootstrap_root_pub=settings.bootstrap_root_public),
            )
        return JSONResponse(status_code=200, content=summary)

    # ---- 管理接口 -------------------------------------------------------

    @app.post("/admin/reset")
    async def admin_reset(request: Request):
        if not settings.admin_token:
            raise HTTPException(status_code=403, detail="管理接口未启用（未设置 ADMIN_TOKEN）")
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""
        if token != settings.admin_token:
            raise HTTPException(status_code=401, detail="管理令牌错误")
        async with update_lock:
            store.reset()
        return {"reset": True}

    return app


app = create_app()
