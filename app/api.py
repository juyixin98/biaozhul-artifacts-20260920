"""FastAPI 接口层：接收源码包/编译配置/编译器输出/部署字节码并给出溯源裁决。

请求格式为 ``multipart/form-data``：
- ``config``          编译配置 JSON（文件或表单字符串，必填）
- ``compilerOutput``  已生成的编译器输出 JSON（文件或表单字符串，必填）
- ``targetDeployed``  链上部署（runtime）字节码 hex（文件或表单字符串，必填）
- ``package``         源码归档（.zip/.tar/.tar.gz，与 sources 至少给一个）
- ``sources``         内联源码 JSON 对象 {"路径": "UTF-8 文本"}（可选）

归档解包严格防御路径穿越/符号链接/压缩炸弹；**不执行包内任何脚本**。
"""

from __future__ import annotations

import hashlib
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .archive import ArchiveError, extract_archive
from .paths import UnsafePath, canonical_source_path
from .service import VerificationRejected, canonical_json, run_verification
from .storage import Storage

MAX_FIELD_BYTES = 2 * 1024 * 1024  # JSON 字段 2 MiB
MAX_PACKAGE_BYTES = 6 * 1024 * 1024  # 源码归档 6 MiB（解压上限 5 MiB）


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "error": {"code": code, "message": message}})


def _json_field(raw: bytes | None, field_name: str):
    if raw is None:
        raise VerificationRejected("MISSING_FIELD", f"缺少必填字段: {field_name}")
    import json

    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise VerificationRejected("BAD_JSON", f"{field_name} 不是合法 JSON: {exc}") from exc


def create_app(storage: Storage | None = None) -> FastAPI:
    app = FastAPI(
        title="Solidity 编译产物离线溯源服务",
        version=__version__,
        description="不调用编译器、不执行上传脚本；逐 nibble 比对并输出哈希链证据。",
    )
    if storage is None:
        data_dir = os.environ.get("PROVENANCE_DATA_DIR", os.path.join(os.getcwd(), "data"))
        storage = Storage(os.path.join(data_dir, "provenance.db"), os.path.join(data_dir, "jobs"))
    app.state.storage = storage

    @app.exception_handler(VerificationRejected)
    async def _rejected_handler(_: Request, exc: VerificationRejected):
        return _err(400, exc.code, exc.message)

    @app.get("/health")
    async def health():
        return {"ok": True, "service": "solc-artifact-provenance", "version": __version__}

    @app.post("/api/v1/verify")
    async def verify(request: Request):
        """每个字段既接受文件部分，也接受普通字符串表单部分。"""
        st: Storage = app.state.storage
        form = await request.form()

        async def _take(name: str) -> tuple[bytes | None, str | None]:
            """返回 (内容字节, 文件名)；字符串部分文件名为 None。"""
            val = form.get(name)
            if val is None:
                return None, None
            if isinstance(val, str):
                return val.encode("utf-8"), None
            data = await val.read(MAX_FIELD_BYTES + 1)
            if len(data) > MAX_FIELD_BYTES:
                raise VerificationRejected("PAYLOAD_TOO_LARGE", f"字段 {name} 超过 {MAX_FIELD_BYTES} 字节上限")
            return data, val.filename

        pkg_field = form.get("package")
        if pkg_field is not None and not isinstance(pkg_field, str):
            pkg_raw = await pkg_field.read(MAX_PACKAGE_BYTES + 1)
            if len(pkg_raw) > MAX_PACKAGE_BYTES:
                raise VerificationRejected(
                    "PAYLOAD_TOO_LARGE", f"package 超过 {MAX_PACKAGE_BYTES} 字节上限"
                )
            pkg_name = pkg_field.filename or "package.zip"
        else:
            pkg_raw, pkg_name = None, "package.zip"

        cfg_raw, _ = await _take("config")
        co_raw, _ = await _take("compilerOutput")
        tgt_raw, _ = await _take("targetDeployed")
        src_raw, _ = await _take("sources")

        config_obj = _json_field(cfg_raw, "config")
        co_obj = _json_field(co_raw, "compilerOutput")
        target_hex = (tgt_raw or b"").decode("ascii", errors="strict").strip()
        if not target_hex:
            raise VerificationRejected("MISSING_FIELD", "缺少必填字段: targetDeployed")

        merged_sources: dict[str, bytes] = {}
        if pkg_raw is not None:
            try:
                extracted = extract_archive(pkg_raw, pkg_name)
            except (ArchiveError, UnsafePath) as exc:
                raise VerificationRejected("BAD_PACKAGE", f"源码包被拒绝: {exc}") from exc
            merged_sources.update(extracted)
        if src_raw is not None:
            inline = _json_field(src_raw, "sources")
            if not isinstance(inline, dict):
                raise VerificationRejected("BAD_SOURCES", "sources 必须是 {路径: 文本} 对象")
            for path, text in inline.items():
                if not isinstance(text, str):
                    raise VerificationRejected("BAD_SOURCES", f"sources[{path!r}] 必须是字符串")
                try:
                    canon = canonical_source_path(path)
                except UnsafePath as exc:
                    raise VerificationRejected("BAD_SOURCES", f"sources[{path!r}] 路径非法: {exc}") from exc
                if canon in merged_sources:
                    raise VerificationRejected(
                        "SOURCE_CONFLICT", f"内联源码与归档条目冲突: {canon}"
                    )
                merged_sources[canon] = text.encode("utf-8")
        if not merged_sources:
            raise VerificationRejected("MISSING_SOURCES", "必须通过 package 或 sources 提供源码")

        # 输入整体摘要：任何字节变化都改变 job 指纹
        input_fingerprint = hashlib.sha256()
        input_fingerprint.update(b"config:")
        input_fingerprint.update(canonical_json(config_obj))
        input_fingerprint.update(b"|compilerOutput:")
        input_fingerprint.update(canonical_json(co_obj))
        input_fingerprint.update("|target:".encode())
        input_fingerprint.update(target_hex.encode())
        for path in sorted(merged_sources):
            input_fingerprint.update(f"|source:{path}:".encode())
            input_fingerprint.update(hashlib.sha256(merged_sources[path]).digest())
        input_sha = input_fingerprint.hexdigest()

        report = run_verification(config_obj, co_obj, merged_sources, target_hex)
        saved = st.save_job(
            sources=merged_sources,
            config_obj=config_obj,
            compiler_output_obj=co_obj,
            target_deployed=target_hex,
            report=report,
            input_sha256=input_sha,
        )
        return {
            "ok": True,
            "job_id": saved["job_id"],
            "created_at": saved["created_at"],
            "input_sha256": input_sha,
            "chain_head": saved["chain_head"],
            "report": report,
        }

    @app.get("/api/v1/jobs")
    async def list_jobs(limit: int = 100):
        storage: Storage = app.state.storage
        limit = max(1, min(limit, 500))
        return {"ok": True, "jobs": storage.list_jobs(limit)}

    @app.get("/api/v1/jobs/{job_id}")
    async def get_job(job_id: str):
        storage: Storage = app.state.storage
        row = storage.get_job(job_id)
        if row is None:
            return _err(404, "NOT_FOUND", f"找不到核验任务: {job_id}")
        return {"ok": True, "job": row}

    @app.get("/api/v1/jobs/{job_id}/report")
    async def get_report(job_id: str):
        storage: Storage = app.state.storage
        report = storage.get_report(job_id)
        if report is None:
            return _err(404, "NOT_FOUND", f"找不到核验任务: {job_id}")
        return {"ok": True, "job_id": job_id, "report": report}

    @app.get("/api/v1/jobs/{job_id}/evidence")
    async def get_evidence(job_id: str):
        storage: Storage = app.state.storage
        chain = storage.verify_chain(job_id)
        if not chain.get("found"):
            return _err(404, "NOT_FOUND", f"找不到核验任务: {job_id}")
        evidence = storage.get_evidence(job_id)
        return {"ok": True, "job_id": job_id, "chain_verification": chain, "evidence": evidence}

    return app


app = create_app()
