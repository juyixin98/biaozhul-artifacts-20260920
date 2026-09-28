"""查询验证层：FastAPI 服务入口。

错误语义：领域错误映射为结构化 JSON（category/message/detail）+ 对应 HTTP 状态码；
未知异常返回 500 + category=INTERNAL，绝不把异常统一包装成成功响应。
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__
from .config import Settings, configure_logging
from .corpus import InputMode
from .errors import MinDfaError
from .service import DfaService


class BuildRequest(BaseModel):
    name: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")
    words: list[str]
    mode: InputMode = InputMode.STRICT


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging((settings or Settings.from_env()).log_level)
    service = DfaService(settings)
    app = FastAPI(title="minidfa", version=__version__)
    app.state.service = service

    @app.exception_handler(MinDfaError)
    async def domain_error_handler(_: Request, exc: MinDfaError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"error": exc.to_payload()})

    @app.exception_handler(Exception)
    async def unknown_error_handler(_: Request, exc: Exception) -> JSONResponse:
        # 未知异常：显式 500，绝不返回成功
        return JSONResponse(
            status_code=500,
            content={"error": {"category": "INTERNAL", "message": str(exc), "detail": {}}},
        )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/automata", status_code=201)
    def build(req: BuildRequest) -> dict[str, object]:
        report = service.build(req.name, req.words, req.mode)
        return {
            "run_id": report.run_id,
            "name": report.name,
            "fingerprint": report.fingerprint,
            "word_count": report.word_count,
            "state_count": report.state_count,
            "edge_count": report.edge_count,
            "elapsed_ms": report.elapsed_ms,
        }

    @app.get("/automata")
    def list_automata() -> dict[str, list[str]]:
        return {"automata": service.store.list_names()}

    @app.get("/automata/{name}/stats")
    def stats(name: str) -> dict[str, object]:
        return service.stats(name)

    @app.get("/automata/{name}/contains")
    def contains(name: str, word: str = "") -> dict[str, object]:
        return {"name": name, "word": word, "accepted": service.contains(name, word)}

    @app.get("/automata/{name}/prefix_count")
    def prefix_count(name: str, prefix: str = "") -> dict[str, object]:
        return {"name": name, "prefix": prefix, "count": service.prefix_count(name, prefix)}

    @app.delete("/automata/{name}", status_code=204)
    def delete(name: str) -> None:
        service.store.delete(name)

    return app


app = create_app()
