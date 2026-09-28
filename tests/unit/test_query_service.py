"""查询服务单元测试。"""

from __future__ import annotations

import pytest

from app.core.dawg import build_dawg
from app.index.model import IndexMetadata
from app.query.errors import IndexNotReadyError, QueryRejectedError
from app.query.service import QueryService, require_loaded

pytestmark = pytest.mark.unit


def _service(words: list[str], *, allow_empty_word: bool = False) -> QueryService:
    dawg = build_dawg(words)
    meta = IndexMetadata(
        index_name="unit",
        schema_version=1,
        allow_empty_word=allow_empty_word,
        source_fixture="",
        build_version="test",
        word_count=dawg.total_words(),
        state_count=len(dawg.states),
        edge_count=sum(len(s.transitions) for s in dawg.states.values()),
    )
    return QueryService(dawg, meta)


def test_membership_member_and_non_member_verdicts() -> None:
    svc = _service(["cat", "cats"])
    member = svc.membership("cat")
    assert member.is_member is True
    assert "accepted" in member.verdict()

    non_member = svc.membership("cab")
    assert non_member.is_member is False
    assert non_member.terminal_state_id is None
    assert "path_break" in non_member.verdict()


def test_prefix_unreachable_is_explicit() -> None:
    svc = _service(["cat"])
    result = svc.prefix_count("z")
    assert result.count == 0
    assert result.reachable is False
    assert "unreachable" in result.verdict()


def test_non_string_query_rejected() -> None:
    svc = _service(["a"])
    with pytest.raises(QueryRejectedError) as exc:
        svc.membership(123)  # type: ignore[arg-type]
    assert exc.value.error_code == "query_rejected"


def test_surrogate_query_rejected() -> None:
    svc = _service(["a"])
    with pytest.raises(QueryRejectedError):
        svc.prefix_count("a\ud800")


def test_empty_query_rejected_when_index_disallows_empty_word() -> None:
    svc = _service(["a"], allow_empty_word=False)
    with pytest.raises(QueryRejectedError) as exc:
        svc.membership("")
    assert exc.value.error_code == "empty_query_rejected"


def test_empty_query_allowed_when_index_allows_empty_word() -> None:
    svc = _service(["", "a"], allow_empty_word=True)
    assert svc.membership("").is_member is True


def test_require_loaded_raises_when_none() -> None:
    with pytest.raises(IndexNotReadyError) as exc:
        require_loaded(None)
    assert exc.value.error_code == "index_not_ready"
    assert exc.value.http_status == 503
