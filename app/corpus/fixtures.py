"""本地合成语料夹具。

所有测试与演示数据都由本模块确定性生成（固定随机种子），不依赖任何
外部账号或真实业务数据。每个夹具带名称与说明，便于测试日志关联输入。
"""

from dataclasses import dataclass

from app.corpus.errors import FixtureNotFoundError
from app.corpus.spec import Word


@dataclass(frozen=True)
class CorpusFixture:
    """一份合成语料。"""

    name: str
    description: str
    words: tuple[Word, ...]

    def as_list(self) -> list[Word]:
        return list(self.words)


# 经典最小性示例词表：
#   共享后缀 "walks"；同时包含前缀关系 "cat" -> "cats"。
# 用它可以直接数出最小自动机的状态数。
FIXTURE_SHARED_SUFFIX = CorpusFixture(
    name="shared_suffix",
    description="共享后缀 -s 与前缀关系 cat/cats 的最小性示例",
    words=("cat", "cats", "dog", "dogs", "walk", "walks"),
)

# 一个词恰好是另一个词前缀的专用夹具。
FIXTURE_PREFIX_WORDS = CorpusFixture(
    name="prefix_words",
    description="词为另一词前缀: be/bee/been/beer",
    words=("be", "bee", "been", "beer", "bees"),
)

# 含重复词的夹具（规范化后应去重）。
FIXTURE_DUPLICATES = CorpusFixture(
    name="duplicates",
    description="含相邻重复词",
    words=("able", "able", "ask", "ask", "ask", "blue"),
)

# 空集合夹具。
FIXTURE_EMPTY = CorpusFixture(
    name="empty",
    description="空词集合",
    words=(),
)

# 含空词的夹具（配合 allow_empty_word 使用）。
FIXTURE_WITH_EMPTY_WORD = CorpusFixture(
    name="with_empty_word",
    description="包含空词；根状态自身终结",
    words=("", "a", "ab"),
)

# 无序夹具，用于验证拒绝/排序两条路径。
FIXTURE_UNORDERED = CorpusFixture(
    name="unordered",
    description="故意乱序的输入",
    words=("banana", "apple", "cherry"),
)

# 单字符符号表，便于手工核对状态合并。
FIXTURE_BINARY = CorpusFixture(
    name="binary_small",
    description="0/1 字母表小语料，覆盖共享前缀与共享后缀",
    words=("0", "01", "011", "10", "110"),
)

ALL_FIXTURES: tuple[CorpusFixture, ...] = (
    FIXTURE_SHARED_SUFFIX,
    FIXTURE_PREFIX_WORDS,
    FIXTURE_DUPLICATES,
    FIXTURE_EMPTY,
    FIXTURE_WITH_EMPTY_WORD,
    FIXTURE_UNORDERED,
    FIXTURE_BINARY,
)

_FIXTURES_BY_NAME = {fx.name: fx for fx in ALL_FIXTURES}


def get_fixture(name: str) -> CorpusFixture:
    try:
        return _FIXTURES_BY_NAME[name]
    except KeyError as exc:
        available = ", ".join(sorted(_FIXTURES_BY_NAME))
        raise FixtureNotFoundError(
            f"未知夹具 {name!r}；可用: {available}"
        ) from exc


def random_corpus(
    *,
    size: int = 200,
    max_word_length: int = 8,
    seed: int = 20260927,
    allow_empty_word: bool = False,
) -> CorpusFixture:
    """生成确定性的随机合成语料（先在集合中生成再排序去重）。

    使用固定种子的本地伪随机生成器，保证测试可复现。返回的词表有序、无重复。
    """
    import random

    if size < 0:
        raise ValueError("size 不能为负")
    if max_word_length < 0:
        raise ValueError("max_word_length 不能为负")

    rng = random.Random(seed)
    alphabet = "abcdefgh"
    collected: set[Word] = set()
    # 生成上限轮数，避免 size 大于可行空间时死循环。
    attempts = 0
    max_attempts = max(size * 50, 1000)
    while len(collected) < size and attempts < max_attempts:
        attempts += 1
        length = rng.randint(1, max_word_length)
        word = "".join(rng.choice(alphabet) for _ in range(length))
        collected.add(word)

    if allow_empty_word:
        collected.add("")

    words = tuple(sorted(collected))
    return CorpusFixture(
        name=f"random_seed{seed}_n{len(words)}",
        description=(
            f"确定性合成语料 seed={seed}, 请求 {size} 词, "
            f"实际 {len(words)} 词, 最长 {max_word_length}"
        ),
        words=words,
    )
