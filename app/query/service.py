"""查询服务：在已加载的不可变索引上做成员判定与前缀计数。

错误语义（显式区分，绝不把异常统一返回成功）：

* 输入不是字符串 / 含代理码位：:class:`QueryRejectedError`；
* 查询空词而索引按固定规则不接受空词：:class:`QueryRejectedError`；
* 索引未构建：:class:`IndexNotReadyError`；
* 词不在词典中是**正常结果** :pyattr:`MembershipStatus.NON_MEMBER`，不是异常；
* 前缀不可达时计数为 0，并显式标记 ``reachable=False``，与"可达但子树恰为
  0 个词"（理论上不会出现，但标记保留判定依据）区分。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

from app.core.dawg import Dawg
from app.index.model import IndexMetadata
from app.query.errors import IndexNotReadyError, QueryRejectedError

EMPTY_QUERY_CODE = "empty_query_rejected"
INVALID_QUERY_CODE = "invalid_query"


class MembershipStatus(str, enum.Enum):
    MEMBER = "member"
    NON_MEMBER = "non_member"


@dataclass(frozen=True)
class MembershipResult:
    """成员判定结果（不可变）。"""

    word: str
    status: MembershipStatus
    terminal_state_id: int | None
    accepted: bool

    @property
    def is_member(self) -> bool:
        return self.status is MembershipStatus.MEMBER

    def verdict(self) -> str:
        """返回判定依据描述，用于诊断日志。"""
        if self.terminal_state_id is None:
            return f"path_break:{self.word!r} 在自动机中途断边"
        if self.status is MembershipStatus.MEMBER:
            return f"accepted:终点状态 {self.terminal_state_id} 为终结状态"
        return f"rejected:终点状态 {self.terminal_state_id} 非终结状态"


@dataclass(frozen=True)
class PrefixCountResult:
    """前缀计数结果（不可变）。"""

    prefix: str
    count: int
    reachable: bool
    terminal_state_id: int | None

    def verdict(self) -> str:
        if not self.reachable:
            return f"unreachable:{self.prefix!r} 路径中断，计数=0"
        return (
            f"counted:终点状态 {self.terminal_state_id} 的子树词数="
            f"{self.count}（终结贡献 + 出边求和）"
        )


class QueryService:
    """对单个已加载索引提供查询。持有只读模型，可跨请求复用。"""

    def __init__(self, dawg: Dawg, metadata: IndexMetadata) -> None:
        self._dawg = dawg
        self._metadata = metadata

    @property
    def metadata(self) -> IndexMetadata:
        return self._metadata

    def _validate(self, text: object) -> str:
        if not isinstance(text, str):
            msg = f"查询输入必须是字符串，实际类型: {type(text).__name__}"
            raise QueryRejectedError(msg)
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
            msg = "查询输入包含 Unicode 代理码位"
            raise QueryRejectedError(msg)
        if text == "" and not self._metadata.allow_empty_word:
            msg = (
                "查询空词被拒绝：该索引按固定规则不接受空词 "
                "（allow_empty_word=False）"
            )
            exc = QueryRejectedError(msg)
            exc.error_code = EMPTY_QUERY_CODE
            raise exc
        return text

    def membership(self, word: object) -> MembershipResult:
        text = self._validate(word)
        end = self._dawg._walk(text)
        accepted = end is not None and self._dawg.states[end].final
        return MembershipResult(
            word=text,
            status=MembershipStatus.MEMBER if accepted else MembershipStatus.NON_MEMBER,
            terminal_state_id=end,
            accepted=accepted,
        )

    def prefix_count(self, prefix: object) -> PrefixCountResult:
        text = self._validate(prefix)
        end = self._dawg._walk(text)
        if end is None:
            return PrefixCountResult(
                prefix=text, count=0, reachable=False, terminal_state_id=None
            )
        return PrefixCountResult(
            prefix=text,
            count=self._dawg.word_counts[end],
            reachable=True,
            terminal_state_id=end,
        )

    def total_words(self) -> int:
        return self._dawg.total_words()


def require_loaded(service: "QueryService | None") -> QueryService:
    """依赖辅助：索引未就绪时给出明确的 503 类别而非成功响应。"""
    if service is None:
        raise IndexNotReadyError("索引尚未构建或加载，请先 POST /admin/build")
    return service
