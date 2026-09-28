"""FastAPI application factory.

Endpoints
---------
``POST /admin/index/build``   build/rebuild an index from a corpus fixture
``GET  /admin/index/meta``    inspect the stored index version/metadata
``POST /query/sorted``        sorted listing (keyset paginated)
``POST /query/range``         strings between two boundaries (sort-key BLOB)
``POST /query/prefix``        collation/text prefix retrieval
``GET  /health``              liveness + library versions

Every response carries a ``trace`` (request id, index version, ordered steps,
categorical failures, uncertainties) and the same trace is emitted as one
JSON log line, so a reproduced failure is explainable end to end.
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ..collation.options import CollationOptions
from ..collation.versioning import InvalidCursor, VersionMismatchError
from ..config import Settings
from ..corpus.loader import load_corpus
from ..index.store import IndexNotFoundError, IndexStore
from ..query.service import PREFIX_COLLATION, PREFIX_TEXT, QueryService
from ..telemetry import Trace, configure_logging
from .schemas import (
    BuildRequest,
    CollationOptionsIn,
    ErrorResponse,
    PrefixBody,
    QueryResponse,
    RangeBody,
    SortedBody,
    StringRow,
    TraceOut,
    validate_option_fields,
)


def options_from_input(data: CollationOptionsIn) -> CollationOptions:
    validate_option_fields(data)
    return CollationOptions(
        locale=data.locale,
        strength=data.strength,
        numeric=data.numeric,
        case_first=data.case_first,
    )


class StoreManager:
    """Resolves a per-option SQLite index file and open stores."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._index_dir = settings.db_path.parent / "indexes"
        self._index_dir.mkdir(parents=True, exist_ok=True)
        self._open: dict[str, IndexStore] = {}

    def open(self, options: CollationOptions) -> IndexStore:
        probe = IndexStore(self._db_path_for(options), options)
        key = probe.expected_version.as_token()
        probe.close()
        if key not in self._open:
            self._open[key] = IndexStore(self._db_path_for(options), options)
        return self._open[key]

    def _db_path_for(self, options: CollationOptions) -> Path:
        # Pre-version identity: options values only, independent of library.
        import hashlib
        import json

        digest = hashlib.sha256(
            json.dumps(options.canonical_dict(), sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        return self._index_dir / f"idx_{digest}.db"

    def close_all(self) -> None:
        for store in self._open.values():
            store.close()
        self._open.clear()


def create_app(settings: Settings | None = None) -> FastAPI:
    configure_logging()
    settings = settings or Settings.from_env()
    manager = StoreManager(settings)
    app = FastAPI(title="collsvc", version="0.1.0")
    app.state.settings = settings
    app.state.manager = manager

    def _error(trace: Trace, category: str, message: str, status: int) -> JSONResponse:
        trace.fail(category, message, "api/app.exception_handler")
        trace.log(logging.WARNING)
        return JSONResponse(
            status_code=status,
            content=ErrorResponse(
                error_category=category,
                error=message,
                trace=TraceOut(**trace.to_dict()),
            ).model_dump(),
        )

    def _row(row) -> StringRow:
        return StringRow(
            doc_id=row["doc_id"],
            text=row["text"],
            sort_key_hex=bytes(row["sort_key"]).hex(),
        )

    def _serve(page, trace: Trace) -> QueryResponse:
        trace.log(logging.INFO)
        return QueryResponse(
            success=True,
            index_version=trace.index_version,
            rows=[_row(r) for r in page.rows],
            page_size=len(page.rows),
            matched=page.matched,
            scanned=page.scanned,
            degraded=page.degraded,
            next_cursor=page.next_cursor,
            trace=TraceOut(**trace.to_dict()),
        )

    @app.get("/health")
    def health() -> dict:
        import icu

        return {
            "status": "ok",
            "icu_version": str(icu.ICU_VERSION),
            "unicode_version": str(icu.UNICODE_VERSION),
        }

    @app.post("/admin/index/build")
    def build_index(body: BuildRequest) -> JSONResponse:
        trace = Trace()
        try:
            options = options_from_input(body.options)
            trace.step(
                "parse_options",
                "api/app.build_index",
                options=options.canonical_dict(),
            )
            corpus_path = settings.corpus_dir / f"{body.corpus_name}.json"
            spec = load_corpus(corpus_path)
            trace.step(
                "load_corpus",
                "api/app.build_index",
                corpus=spec.name,
                entries=len(spec.entries),
            )
            db_path = manager._db_path_for(options)
            store = IndexStore(db_path, options)
            try:
                result = store.build(spec, replace=body.replace)
            finally:
                manager._open.pop(store.expected_version.as_token(), None)
                store.close()
            trace.bind_version(result["index_version"])
            trace.step(
                "build_complete",
                "api/app.build_index",
                status=result["status"],
                row_count=result.get("row_count"),
            )
            trace.log(logging.INFO)
            return JSONResponse(
                content={
                    "success": True,
                    "result": result,
                    "trace": trace.to_dict(),
                }
            )
        except FileNotFoundError as exc:
            return _error(trace, "CORPUS_NOT_FOUND", str(exc), 404)
        except Exception as exc:  # mapped below by class
            category, status = _classify(exc)
            return _error(trace, category, str(exc), status)

    @app.get("/admin/index/meta")
    def meta(locale: str = "en_US", strength: int = 3,
             numeric: bool = False, case_first: str = "default") -> JSONResponse:
        trace = Trace()
        try:
            options = options_from_input(
                CollationOptionsIn(
                    locale=locale,
                    strength=strength,
                    numeric=numeric,
                    case_first=case_first,
                )
            )
            store = manager.open(options)
            data = store.require_open()
            trace.bind_version(data["index_version"])
            trace.step("read_meta", "api/app.meta", row_count=data.get("row_count"))
            trace.log(logging.INFO)
            return JSONResponse(
                content={"success": True, "built": True, "meta": data, "trace": trace.to_dict()}
            )
        except Exception as exc:
            category, status = _classify(exc)
            return _error(trace, category, str(exc), status)

    def _service(options_in: CollationOptionsIn) -> tuple[QueryService, Trace]:
        options = options_from_input(options_in)
        store = manager.open(options)
        trace = Trace()
        service = QueryService(store)
        trace.bind_version(service.index_version)
        return service, trace

    def _limit(requested: int) -> int:
        if requested < 1:
            raise ValueError("limit must be >= 1")
        return min(requested, settings.page_size_max)

    @app.post("/query/sorted")
    def query_sorted(body: SortedBody):
        trace = Trace()
        try:
            service, trace = _service(body.options)
            page = service.list_sorted(_limit(body.limit), body.cursor, trace)
            return _serve(page, trace)
        except Exception as exc:
            category, status = _classify(exc)
            return _error(trace, category, str(exc), status)

    @app.post("/query/range")
    def query_range(body: RangeBody):
        trace = Trace()
        try:
            service, trace = _service(body.options)
            page = service.range_between(
                body.low, body.high, _limit(body.limit), body.cursor, trace
            )
            return _serve(page, trace)
        except Exception as exc:
            category, status = _classify(exc)
            return _error(trace, category, str(exc), status)

    @app.post("/query/prefix")
    def query_prefix(body: PrefixBody):
        trace = Trace()
        try:
            if body.match not in (PREFIX_COLLATION, PREFIX_TEXT):
                raise InvalidCursor(
                    f"match must be {PREFIX_COLLATION!r} or {PREFIX_TEXT!r}"
                )
            service, trace = _service(body.options)
            page = service.prefix_search(
                body.prefix, body.match, _limit(body.limit), body.cursor, trace
            )
            return _serve(page, trace)
        except Exception as exc:
            category, status = _classify(exc)
            return _error(trace, category, str(exc), status)

    return app


def _classify(exc: Exception) -> tuple[str, int]:
    from ..collation.engine import LocaleNotSupportedError
    from ..corpus.spec import InvalidCorpusSpec

    if isinstance(exc, VersionMismatchError):
        return "INDEX_VERSION_CONFLICT", 409
    if isinstance(exc, InvalidCursor):
        return "INVALID_CURSOR", 400
    if isinstance(exc, LocaleNotSupportedError):
        return "UNSUPPORTED_LOCALE", 400
    if isinstance(exc, InvalidCorpusSpec):
        return "INVALID_CORPUS_SPEC", 400
    if isinstance(exc, IndexNotFoundError):
        return "INDEX_NOT_BUILT", 409
    if isinstance(exc, ValueError):
        return "INVALID_ARGUMENT", 400
    return "INTERNAL_ERROR", 500
