"""请求与响应 schema（分离）。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class BuildRequest(BaseModel):
    """构建索引请求。"""

    words: list[str] = Field(
        default_factory=list, description="词典词项；默认要求有序非递减"
    )
    allow_empty_word: bool = Field(
        default=False, description="是否接受空词（固定规则，默认拒绝）"
    )
    sort_first: bool = Field(
        default=False, description="输入无序时是否先排序而不是拒绝"
    )
    index_name: str | None = Field(default=None, description="索引名称")
    fixture: str | None = Field(
        default=None, description="若提供，则使用命名的本地合成夹具而忽略 words"
    )


class BuildResponse(BaseModel):
    success: bool
    index_name: str
    input_count: int
    unique_count: int
    duplicate_count: int
    raw_states_created: int
    state_count: int
    edge_count: int
    merge_count: int
    sorted_by_service: bool
    word_count: int


class MembershipResponse(BaseModel):
    success: bool = True
    word: str
    member: bool
    status: str
    terminal_state_id: int | None
    verdict: str


class PrefixCountResponse(BaseModel):
    success: bool = True
    prefix: str
    count: int
    reachable: bool
    terminal_state_id: int | None
    verdict: str


class StatsResponse(BaseModel):
    success: bool = True
    ready: bool
    index_name: str | None = None
    word_count: int | None = None
    state_count: int | None = None
    edge_count: int | None = None
    allow_empty_word: bool | None = None
    source_fixture: str | None = None
    build_version: str | None = None


class ErrorResponse(BaseModel):
    """统一错误信封：绝不把异常返回成 success=true。"""

    success: bool = False
    error_code: str
    message: str
    http_status: int
    details: list[str] = Field(default_factory=list)
    run_id: str | None = None


class HealthResponse(BaseModel):
    success: bool = True
    status: str
    version: str
    run_id: str
