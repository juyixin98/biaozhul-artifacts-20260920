"""FastAPI 应用工厂与薄路由。

错误语义：

* 语料/查询的可预期拒绝（无序、空词、非法输入）映射为 4xx，响应体
  ``success=false`` 并带固定 ``error_code``；
* 持久化完整性违规（环、悬空状态等）映射为 500，携带每项违规的类别与
  判定依据；
* 未建索引查询返回 503；
* 未预期异常返回 500，同样 ``success=false``，绝不伪装成功。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app import __version__
from app.api.deps import AppState, get_index_service, get_query_service, get_state
from app.api.schemas import (
    BuildRequest,
    BuildResponse,
    ErrorResponse,
    HealthResponse,
    MembershipResponse,
    PrefixCountResponse,
    StatsResponse,
)
from app.config import SETTINGS, Settings
from app.corpus.errors import CorpusError
from app.corpus.fixtures import get_fixture
from app.corpus.spec import CorpusSpec
from app.diagnostics import configure_logging
from app.index.errors import IndexError, IndexIntegrityError
from app.index.repository import SQLiteIndexRepository
from app.index.service import IndexService
from app.query.errors import QueryError
from app.query.service import QueryService

logger = logging.getLogger(__name__)


def _error_envelope(
    *,
    code: str,
    message: str,
    http_status: int,
    details: list[str] | None = None,
    run_id: str | None = None,
) -> JSONResponse:
    payload = ErrorResponse(
        error_code=code,
        message=message,
        http_status=http_status,
        details=details or [],
        run_id=run_id,
    )
    return JSONResponse(status_code=http_status, content=payload.model_dump())


def install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(CorpusError)
    async def _handle_corpus(_: Request, exc: CorpusError) -> JSONResponse:
        state = getattr(_.app.state, "app_state", None)
        run_id = state.run_id if state else None
        logger.warning("corpus rejection code=%s detail=%s", exc.error_code, str(exc))
        return _error_envelope(
            code=exc.error_code,
            message=str(exc),
            http_status=exc.http_status,
            run_id=run_id,
        )

    @app.exception_handler(QueryError)
    async def _handle_query(_: Request, exc: QueryError) -> JSONResponse:
        state = getattr(_.app.state, "app_state", None)
        run_id = state.run_id if state else None
        logger.warning("query rejection code=%s detail=%s", exc.error_code, str(exc))
        return _error_envelope(
            code=exc.error_code,
            message=str(exc),
            http_status=exc.http_status,
            run_id=run_id,
        )

    @app.exception_handler(IndexIntegrityError)
    async def _handle_integrity(_: Request, exc: IndexIntegrityError) -> JSONResponse:
        state = getattr(_.app.state, "app_state", None)
        run_id = state.run_id if state else None
        logger.error("index integrity violation: %s", str(exc))
        return _error_envelope(
            code=exc.error_code,
            message="持久化引用完整性校验失败",
            http_status=exc.http_status,
            details=[f"[{v.kind}] {v.detail}" for v in exc.violations],
            run_id=run_id,
        )

    @app.exception_handler(IndexError)
    async def _handle_index(_: Request, exc: IndexError) -> JSONResponse:
        state = getattr(_.app.state, "app_state", None)
        run_id = state.run_id if state else None
        logger.error("index error code=%s detail=%s", exc.error_code, str(exc))
        return _error_envelope(
            code=exc.error_code,
            message=str(exc),
            http_status=exc.http_status,
            run_id=run_id,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        state = getattr(_.app.state, "app_state", None)
        run_id = state.run_id if state else None
        logger.exception("unexpected error: %s", exc)
        return _error_envelope(
            code="internal_error",
            message=f"未预期的服务端错误: {type(exc).__name__}: {exc}",
            http_status=500,
            run_id=run_id,
        )


def create_app(
    *,
    settings: Settings | None = None,
    run_id: str | None = None,
    autoload: bool = True,
) -> FastAPI:
    """构造 FastAPI 应用（测试可注入 settings/run_id 并关闭自动加载）。"""
    settings = settings or SETTINGS
    run_id = configure_logging(settings.log_dir, settings.log_level, run_id)

    repository = SQLiteIndexRepository(settings.db_path)
    index_service = IndexService(repository)
    app_state = AppState(
        settings=settings,
        repository=repository,
        index_service=index_service,
        run_id=run_id,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        if autoload and repository.exists():
            try:
                persisted = index_service.load()
                app_state.set_loaded(
                    QueryService(persisted.dawg, persisted.metadata)
                )
                logger.info(
                    "autoloaded index name=%s words=%d",
                    persisted.metadata.index_name,
                    persisted.metadata.word_count,
                )
            except IndexIntegrityError:
                # 损坏的持久化文件不应阻止服务启动；查询端点会返回 503/500。
                logger.error("autoload skipped due to integrity violation")
        yield

    app = FastAPI(
        title="MADFA / DAWG 不可变有序词典服务",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.app_state = app_state
    install_exception_handlers(app)

    @app.get("/health", response_model=HealthResponse, tags=["meta"])
    async def health() -> HealthResponse:
        return HealthResponse(status="ok", version=__version__, run_id=run_id)

    @app.post("/admin/build", response_model=BuildResponse, tags=["admin"])
    async def build(
        request: BuildRequest,
        svc: IndexService = Depends(get_index_service),
        state: AppState = Depends(get_state),
    ) -> BuildResponse:
        if request.fixture:
            # 未知夹具抛 FixtureNotFoundError(404)，由 CorpusError 处理器统一返回。
            fixture = get_fixture(request.fixture)
            words = fixture.as_list()
            source = fixture.name
        else:
            words = request.words
            source = None

        spec = CorpusSpec(
            allow_empty_word=request.allow_empty_word,
            sort_first=request.sort_first,
        )
        name = request.index_name or (source or state.settings.default_index_name)
        dawg, report = svc.build_from_words(
            words, index_name=name, spec=spec, source_fixture=source
        )
        state.set_loaded(QueryService(dawg, report.metadata))
        return BuildResponse(
            success=True,
            index_name=report.index_name,
            input_count=report.input_count,
            unique_count=report.unique_count,
            duplicate_count=report.duplicate_count,
            raw_states_created=report.raw_states_created,
            state_count=report.metadata.state_count,
            edge_count=report.metadata.edge_count,
            merge_count=report.merge_count,
            sorted_by_service=report.sorted_by_service,
            word_count=report.metadata.word_count,
        )

    @app.get("/stats", response_model=StatsResponse, tags=["query"])
    async def stats(state: AppState = Depends(get_state)) -> StatsResponse:
        if state.query_service is None:
            return StatsResponse(ready=False)
        meta = state.query_service.metadata
        return StatsResponse(
            ready=True,
            index_name=meta.index_name,
            word_count=meta.word_count,
            state_count=meta.state_count,
            edge_count=meta.edge_count,
            allow_empty_word=meta.allow_empty_word,
            source_fixture=meta.source_fixture or None,
            build_version=meta.build_version,
        )

    @app.get(
        "/query/membership",
        response_model=MembershipResponse,
        tags=["query"],
    )
    async def membership(
        word: str,
        svc: QueryService = Depends(get_query_service),
    ) -> MembershipResponse:
        result = svc.membership(word)
        return MembershipResponse(
            word=result.word,
            member=result.is_member,
            status=result.status.value,
            terminal_state_id=result.terminal_state_id,
            verdict=result.verdict(),
        )

    @app.get(
        "/query/prefix-count",
        response_model=PrefixCountResponse,
        tags=["query"],
    )
    async def prefix_count(
        prefix: str,
        svc: QueryService = Depends(get_query_service),
    ) -> PrefixCountResponse:
        result = svc.prefix_count(prefix)
        return PrefixCountResponse(
            prefix=result.prefix,
            count=result.count,
            reachable=result.reachable,
            terminal_state_id=result.terminal_state_id,
            verdict=result.verdict(),
        )

    return app


# 模块级应用供 ``uvicorn app.api.app:app`` 使用。
app = create_app()
