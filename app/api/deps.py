"""应用状态与依赖注入。"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from app.config import Settings
from app.index.repository import SQLiteIndexRepository
from app.index.service import IndexService
from app.query.service import QueryService, require_loaded


@dataclass
class AppState:
    """进程内应用状态（单索引）。

    ``query_service`` 在构建/加载前为 ``None``；查询依赖会显式抛出
    503 而不是伪装成成功。
    """

    settings: Settings
    repository: SQLiteIndexRepository
    index_service: IndexService
    run_id: str
    query_service: QueryService | None = None

    def set_loaded(self, query_service: QueryService) -> None:
        self.query_service = query_service

    def reset_loaded(self) -> None:
        self.query_service = None


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.app_state
    return state


def get_settings(request: Request) -> Settings:
    return get_state(request).settings


def get_index_service(request: Request) -> IndexService:
    return get_state(request).index_service


def get_query_service(request: Request) -> QueryService:
    state = get_state(request)
    # require_loaded 在未就绪时抛 IndexNotReadyError（503）。
    return require_loaded(state.query_service)


def get_run_id(request: Request) -> str:
    return get_state(request).run_id
