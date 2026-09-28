"""FastAPI HTTP 层。

路由
----
* ``GET  /health``                 健康检查（含版本）
* ``GET  /models``                 已加载模型列表
* ``POST /models/ingest``          摄取本地语料文件
* ``POST /query``                  n-最短转换查询
* ``POST /query/cross-check``      组合 vs 分阶段顺序执行交叉核验

统一响应信封 ``{success, data, error}``；失败时 ``success=false`` 且
``error.category`` 为明确类别，HTTP 状态码与类别对应，绝不把异常伪装成
成功。预算截断是**非致命**的未完成状态：``success=true`` 但
``data.complete=false``、``data.status="incomplete"``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from wfst import __version__
from wfst.config import Settings
from wfst.corpus.spec import CorpusValidationError
from wfst.manager import ModelManager
from wfst.query import (
    ErrorCategory,
    QueryError,
    render_output,
    run_query,
    transduce_stagewise,
)
from wfst.service.logging import RunLogger, new_run_id

_HTTP_STATUS = {
    ErrorCategory.MODEL_NOT_FOUND: 404,
    ErrorCategory.EMPTY_INPUT: 422,
    ErrorCategory.INPUT_TOO_LONG: 422,
    ErrorCategory.UNKNOWN_SYMBOL: 422,
    ErrorCategory.INVALID_PARAMETER: 422,
    ErrorCategory.UNACCEPTABLE_INPUT: 422,
    ErrorCategory.NEGATIVE_CYCLE: 422,
}


class QueryRequest(BaseModel):
    corpus_id: str = Field(min_length=1)
    version: str | None = None
    input: str = Field(min_length=0)
    k: int = Field(default=5, ge=1, le=100)
    budget: int = Field(default=200_000, ge=1, le=5_000_000)
    # composed=一次性组合；stagewise=分阶段顺序执行。
    # 需要两路同时比对请用 POST /query/cross-check。
    mode: str = Field(default="composed", pattern="^(composed|stagewise)$")


class IngestRequest(BaseModel):
    path: str = Field(min_length=1)


def _hyp_payload(resp) -> list[dict]:
    return [
        {
            "output": render_output(h.output, resp.token_level),
            "cost": round(h.cost, 6),
        }
        for h in resp.hypotheses
    ]


def create_app(settings: Settings | None = None,
               manager: ModelManager | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    from wfst.index.store import Store

    store = Store(settings.db_path)
    manager = manager or ModelManager(store, settings)
    logger = RunLogger(settings.log_dir)
    app = FastAPI(title="小型加权有限状态转换器服务", version=__version__)
    app.state.settings = settings
    app.state.manager = manager
    app.state.logger = logger

    def error_envelope(category: ErrorCategory, message: str,
                       details: dict, run_id: str | None, http_status: int,
                       inputs: dict | None) -> JSONResponse:
        logger.event(
            run_id or new_run_id(),
            "query_error",
            category=category.value,
            message=message,
            **(inputs or {}),
            verdict=f"error:{category.value}",
        )
        return JSONResponse(
            status_code=http_status,
            content={
                "success": False,
                "data": None,
                "error": {
                    "category": category.value,
                    "message": message,
                    "details": details,
                    "run_id": run_id,
                },
            },
        )

    @app.get("/health")
    def health() -> dict:
        return {"success": True, "data": {"status": "ok",
                                          "service_version": __version__}}

    @app.get("/models")
    def models() -> dict:
        return {"success": True, "data": {"models": manager.list_models()}}

    @app.post("/models/ingest")
    def ingest(req: IngestRequest) -> JSONResponse:
        run_id = new_run_id()
        path = Path(req.path)
        try:
            result = manager.ingest_file(path)
        except FileNotFoundError:
            return error_envelope(
                ErrorCategory.INVALID_PARAMETER,
                f"语料文件不存在：{path}", {"path": str(path)}, run_id, 422,
                inputs={"path": str(path)},
            )
        except CorpusValidationError as exc:
            logger.event(
                run_id, "ingest_error", category="corpus_validation",
                errors=exc.errors, verdict="error:corpus_validation",
            )
            return JSONResponse(
                status_code=422,
                content={
                    "success": False, "data": None,
                    "error": {"category": "corpus_validation",
                              "message": str(exc), "errors": exc.errors,
                              "run_id": run_id},
                },
            )
        logger.event(
            run_id, "ingest_ok", corpus=result.model.corpus_id,
            version=result.model.version, fingerprint=result.fingerprint,
            pipeline_states=result.model.pipeline.num_states,
            verdict="ok",
        )
        return JSONResponse(
            status_code=201,
            content={
                "success": True,
                "data": {
                    "corpus_id": result.model.corpus_id,
                    "version": result.model.version,
                    "fingerprint": result.fingerprint,
                    "model": {
                        "token_level": result.model.token_level,
                        "alphabet_size": len(result.model.alphabet),
                        "pipeline_states": result.model.pipeline.num_states,
                        "pipeline_arcs": len(result.model.pipeline.arcs),
                    },
                    "mining_steps": result.report.steps(),
                    "run_id": run_id,
                },
            },
        )

    def _execute(mode: str, model, req: QueryRequest):
        if mode == "composed":
            return run_query(
                model, req.input, k=req.k, budget=req.budget,
                max_input_len=settings.max_input_len,
            )
        return transduce_stagewise(
            model, req.input, k=req.k, budget=req.budget,
            max_input_len=settings.max_input_len,
        )

    def _answer(req: QueryRequest, run_id: str, mode_label: str) -> Any:
        model = manager.get(req.corpus_id, req.version)
        resp = _execute(mode_label, model, req)
        outputs = _hyp_payload(resp)
        status = "complete" if resp.complete else "incomplete"
        verdict = "ok" if resp.complete else "incomplete:budget_exhausted"
        logger.event(
            run_id, "query_ok",
            corpus=f"{model.corpus_id}@{model.version}",
            service_version=__version__, input=req.input, mode=mode_label,
            k=req.k, pops=resp.pops, budget=req.budget, complete=resp.complete,
            outputs=[o["output"] for o in outputs], verdict=verdict,
            steps=resp.trace,
        )
        return {
            "run_id": run_id,
            "corpus_id": model.corpus_id,
            "version": model.version,
            "mode": mode_label,
            "status": status,
            "complete": resp.complete,
            "pops": resp.pops,
            "budget": req.budget,
            "hypotheses": outputs,
            "trace": resp.trace,
        }

    @app.post("/query")
    def query(req: QueryRequest) -> JSONResponse:
        run_id = new_run_id()
        try:
            data = _answer(req, run_id, req.mode)
        except QueryError as exc:
            return error_envelope(
                exc.category, str(exc), exc.details, run_id,
                _HTTP_STATUS[exc.category],
                inputs={"corpus": req.corpus_id, "input": req.input},
            )
        return JSONResponse(content={"success": True, "data": data})

    @app.post("/query/cross-check")
    def cross_check(req: QueryRequest) -> JSONResponse:
        run_id = new_run_id()
        try:
            composed = _answer(req, run_id, "composed")
            staged = _answer(req, run_id, "stagewise")
        except QueryError as exc:
            return error_envelope(
                exc.category, str(exc), exc.details, run_id,
                _HTTP_STATUS[exc.category],
                inputs={"corpus": req.corpus_id, "input": req.input},
            )

        def signature(data: dict) -> list[tuple[str, float]]:
            return [(h["output"], h["cost"]) for h in data["hypotheses"]]

        agree = signature(composed) == signature(staged)
        logger.event(
            run_id, "cross_check",
            corpus=composed["corpus_id"], input=req.input,
            composed=signature(composed), stagewise=signature(staged),
            verdict="agree" if agree else "MISMATCH",
        )
        status = 200 if agree else 409
        return JSONResponse(
            status_code=status,
            content={
                "success": agree,
                "data": {
                    "run_id": run_id,
                    "agree": agree,
                    "composed": composed,
                    "stagewise": staged,
                },
                "error": None if agree else {
                    "category": "cross_check_mismatch",
                    "message": "组合与分阶段顺序执行结果不一致",
                },
            },
        )

    return app


def create_default_app() -> FastAPI:
    """从环境变量构建设置，并自动摄取 fixtures 目录（若内存中尚无模型）。"""
    settings = Settings.from_env()
    app = create_app(settings)
    fixtures_dir = Path(__file__).resolve().parents[3] / "fixtures" / "corpora"
    manager: ModelManager = app.state.manager
    if not manager.list_models() and fixtures_dir.exists():
        for path in sorted(fixtures_dir.glob("*.json")):
            manager.ingest_file(path)
    return app


# 懒加载：仅当 uvicorn（或调用方）真正访问模块属性 ``app`` 时才构建默认应用。
# 这样「import 该模块」（例如测试中引用 create_app / transduce_stagewise）
# 不会产生建库、摄取夹具等副作用。uvicorn 的 "wfst.service.app:app" 目标
# 会触发 __getattr__，行为不变。
_app_instance: FastAPI | None = None


def get_app() -> FastAPI:
    global _app_instance
    if _app_instance is None:
        _app_instance = create_default_app()
    return _app_instance


def __getattr__(name: str):
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
