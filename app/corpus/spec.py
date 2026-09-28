"""语料规范：词项合法性与有序性。

规则（固定契约）：

* 词项必须是 ``str``，且不得包含 Unicode 代理码位；
* **空词规则固定**：默认拒绝空词 ``""``；只有显式构造
  ``CorpusSpec(allow_empty_word=True)`` 时才接受。是否接受空词必须由
  调用方在构建索引前明确声明，不允许静默猜测；
* 构建 DAWG 要求输入按字典序（Unicode 码位序）**非递减**排列：

  - 重复词（相邻相等）允许出现，规范化时去重并计数；
  - 一旦出现逆序（``words[i] > words[i+1]``），抛出
    :class:`UnorderedCorpusError`；
  - 调用方也可以显式要求先排序（``sort_first=True``）。
"""

from dataclasses import dataclass, field

from app.corpus.errors import (
    EmptyWordError,
    InvalidWordError,
    UnorderedCorpusError,
)

Word = str
"""词典中的一个词。"""


def accept_empty_word(allow: bool) -> bool:
    """返回空词规则的判定结果。

    规则固定：空词仅在调用方显式允许时接受。这个函数把规则写成显式谓词，
    便于在测试中直接断言判定依据。
    """
    return bool(allow)


def _validate_word(word: object, allow_empty: bool) -> Word:
    if not isinstance(word, str):
        msg = f"词项必须是字符串，实际类型: {type(word).__name__}"
        raise InvalidWordError(msg)
    if any(0xD800 <= ord(ch) <= 0xDFFF for ch in word):
        msg = "词项包含 Unicode 代理码位，无法可靠持久化"
        raise InvalidWordError(msg)
    if word == "" and not accept_empty_word(allow_empty):
        msg = "空词被固定规则拒绝；如需接受请显式声明 allow_empty_word=True"
        raise EmptyWordError(msg)
    return word


def check_sorted(words: list[Word]) -> None:
    """检查词表是否非递减有序；逆序时抛出 :class:`UnorderedCorpusError`。

    重复（相等相邻）不算逆序。
    """
    for index in range(1, len(words)):
        if words[index - 1] > words[index]:
            msg = (
                f"词表不是有序输入：位置 {index - 1} 的 {words[index - 1]!r} "
                f"大于位置 {index} 的 {words[index]!r}；请先排序或显式声明 "
                "sort_first=True"
            )
            raise UnorderedCorpusError(msg)


@dataclass(frozen=True)
class CorpusSpec:
    """语料规范配置（不可变）。

    :param allow_empty_word: 是否接受空词，默认 ``False``（固定规则）。
    :param sort_first: 无序输入时是否先排序而不是拒绝，默认 ``False``。
    """

    allow_empty_word: bool = False
    sort_first: bool = False

    def validate_word(self, word: object) -> Word:
        return _validate_word(word, self.allow_empty_word)

    def normalize(self, words: object) -> "NormalizedCorpus":
        """校验并规范化输入词表。

        :returns: :class:`NormalizedCorpus`，含去重后的有序词表与统计。
        :raises InvalidWordError: 词项类型或字符非法。
        :raises EmptyWordError: 出现空词且规范拒绝空词。
        :raises UnorderedCorpusError: 输入逆序且未允许先排序。
        """
        if not isinstance(words, (list, tuple)):
            msg = f"词表必须是 list 或 tuple，实际类型: {type(words).__name__}"
            raise InvalidWordError(msg)

        validated: list[Word] = [self.validate_word(w) for w in words]

        input_count = len(validated)
        if self.sort_first:
            validated = sorted(validated)
        else:
            check_sorted(validated)

        unique: list[Word] = []
        duplicate_count = 0
        for word in validated:
            if unique and unique[-1] == word:
                duplicate_count += 1
                continue
            unique.append(word)

        return NormalizedCorpus(
            words=unique,
            spec=self,
            input_count=input_count,
            duplicate_count=duplicate_count,
            was_sorted_by_us=self.sort_first,
        )


@dataclass(frozen=True)
class NormalizedCorpus:
    """规范化结果（不可变）。"""

    words: list[Word] = field(default_factory=list)
    spec: CorpusSpec = field(default_factory=CorpusSpec)
    input_count: int = 0
    duplicate_count: int = 0
    was_sorted_by_us: bool = False

    @property
    def unique_count(self) -> int:
        return len(self.words)


def normalize_words(
    words: object,
    *,
    allow_empty_word: bool = False,
    sort_first: bool = False,
) -> NormalizedCorpus:
    """便捷函数：按给定规范规范化词表。"""
    spec = CorpusSpec(
        allow_empty_word=allow_empty_word,
        sort_first=sort_first,
    )
    return spec.normalize(words)
