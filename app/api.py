"""FastAPI 接口层：请求身份关联、可解释错误、启动配置。

启动行为：
- 索引为空且语料可用 → 自动首次构建；
- 索引版本与内核版本不一致（规则升级）→ 不自动重建，查询返回
  409 INDEX_VERSION_CONFLICT，须显式 POST /admin/rebuild。
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from .config import CollationRules, corpus_path_from_env, db_path_from_env, load_rules_from_env
from .corpus import canonical_equivalence_groups, load_corpus, validate_entries
from .errors import ErrorCategory, ServiceError, entry_not_found
from .index import SQLiteIndex
from .kernel import CollationKernel
from .logging_setup import configure_logging, get_logger, log_failure, log_step
from .models import (
    EntryModel,
    ErrorEnvelope,
    RebuildResult,
    VersionInfo,
)
from .query import MAX_LIMIT, QueryService


def create_app(
    rules: CollationRules | None = None,
    *,
    db_path: str | None = None,
    corpus_path: str | None = None,
) -> FastAPI:
    """应用工厂：测试与生产共用，配置显式注入。"""
    configure_logging()
    rules = rules if rules is not None else load_rules_from_env()
    db_path = db_path if db_path is not None else db_path_from_env()
    corpus_path = corpus_path if corpus_path is not None else corpus_path_from_env()

    kernel = CollationKernel(rules)
    index = SQLiteIndex(db_path)
    queries = QueryService(kernel, index)
    logger = get_logger()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        stored = index.stored_version()
        if stored is None:
            entries = load_corpus(corpus_path)
            count = index.rebuild(kernel, entries)
            log_step(
                "startup", "auto_build",
                index_version=kernel.index_version, entries=count, corpus=corpus_path,
            )
        elif stored != kernel.index_version:
            log_failure(
                "startup",
                ErrorCategory.INDEX_VERSION_CONFLICT.value,
                "索引版本与内核规则不一致，等待显式重建",
                stored_version=stored,
                kernel_version=kernel.index_version,
            )
        else:
            log_step("startup", "index_ready", index_version=stored)
        yield
        index.close()

    app = FastAPI(title="collationsvc", version="0.1.0", lifespan=lifespan)

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        log_step(request_id, "request", method=request.method, path=request.url.path)
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    @app.exception_handler(ServiceError)
    async def service_error_handler(request: Request, exc: ServiceError):
        request_id = getattr(request.state, "request_id", "unknown")
        log_failure(
            request_id, exc.category.value, exc.message,
            http_status=exc.http_status, **exc.detail,
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=ErrorEnvelope(
                error={
                    "category": exc.category.value,
                    "message": exc.message,
                    "detail": {k: str(v) for k, v in exc.detail.items()},
                    "request_id": request_id,
                }
            ).model_dump(),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        request_id = getattr(request.state, "request_id", "unknown")
        logger.exception(
            "request_id=%s step=failure category=INTERNAL", request_id, exc_info=exc
        )
        return JSONResponse(
            status_code=500,
            content=ErrorEnvelope(
                error={
                    "category": ErrorCategory.INTERNAL.value,
                    "message": "未预期错误，详见服务端日志",
                    "detail": {},
                    "request_id": request_id,
                }
            ).model_dump(),
        )

    # ---- 端点 ----

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/version", response_model=VersionInfo)
    def version(request: Request) -> VersionInfo:
        stored = index.stored_version()
        return VersionInfo(
            index_version=stored,
            kernel_version=kernel.index_version,
            rules=rules.canonical_dict(),
            icu_version=kernel.icu_version,
            entry_count=index.count(),
            needs_rebuild=stored is not None and stored != kernel.index_version,
            request_id=request.state.request_id,
        )

    @app.get("/entries/sorted")
    def entries_sorted(
        request: Request,
        limit: int = Query(default=50),
        cursor: str | None = Query(default=None),
    ):
        return queries.sorted_page(limit, cursor, request.state.request_id)

    @app.get("/entries/range")
    def entries_range(
        request: Request,
        lower: str = Query(...),
        upper: str = Query(...),
        lower_inclusive: bool = Query(default=True),
        upper_inclusive: bool = Query(default=True),
        limit: int = Query(default=MAX_LIMIT),
    ):
        return queries.range_query(
            lower,
            upper,
            lower_inclusive=lower_inclusive,
            upper_inclusive=upper_inclusive,
            limit=limit,
            request_id=request.state.request_id,
        )

    @app.get("/entries/{entry_id}", response_model=EntryModel)
    def entry_by_id(request: Request, entry_id: str) -> EntryModel:
        version = index.require_version(kernel)
        row = index.get(entry_id)
        if row is None:
            raise entry_not_found("条目不存在", id=entry_id, index_version=version)
        return EntryModel(
            id=row["id"],
            seq=row["seq"],
            original=row["original"],
            nfc=row["nfc"],
            sort_key_hex=row["sort_key"].hex(),
        )

    @app.post("/admin/rebuild", response_model=RebuildResult)
    def rebuild(request: Request, body: dict | None = None) -> RebuildResult:
        if body and "entries" in body:
            entries = validate_entries(body["entries"])
            source = "request_body"
        else:
            entries = load_corpus(corpus_path)
            source = corpus_path
        count = index.rebuild(kernel, entries)
        groups = canonical_equivalence_groups(entries)
        log_step(
            request.state.request_id,
            "rebuild",
            index_version=kernel.index_version,
            entries=count,
            source=source,
            equivalence_groups=len(groups),
        )
        return RebuildResult(
            index_version=kernel.index_version,
            entry_count=count,
            canonical_equivalence_groups=groups,
            request_id=request.state.request_id,
        )

    return app


app = create_app()
