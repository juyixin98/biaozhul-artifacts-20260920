"""显式定义的分词器（抽取与搜索共用，保证索引/查询一致）。

规则：
1. CJK 统一表意文字（U+4E00–U+9FFF）及扩展 A 区逐字成词；
2. 拉丁字母/数字按连续串成词，保留 snake_case / camelCase / x.y.z / v1.2 等
   标识符形态（点和下划线留在串内，如 ``sqlalchemy_orm``、``python 3.12`` 中的
   ``3.12``）；
3. 其他空白与标点为分隔符；
4. 拉丁串小写化；纯数字串保留但也小写（无影响）。

注：camelCase 的内部切分不做——整串 ``lowercamelcase`` 是一个词，同时逐字 CJK
保证中文按字可检索。多字中文检索由查询侧 AND 组合完成。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN_RE = re.compile(
    r"[㐀-䶿一-鿿]"
    r"|[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*"
)

_CJK_RANGES = ((0x3400, 0x4DBF), (0x4E00, 0x9FFF))

# 显式停用词表（可解释、可审计）：高频功能字/虚词与英文功能词。
# 停用词同时从索引和查询中剔除，因此不影响命中判定的一致性；
# 人名/组织等实体识别直接扫描原文，不受此表影响。
STOPWORDS: frozenset[str] = frozenset(
    """的 了 在 是 我 有 和 就 不 人 都 一 上 也 很 到 说 要 去 你 会 着 没 看 好 这 那
       与 及 或 中 等 地 得 把 被 让 向 从 对 为 以 于 其 之 吗 呢 吧 啊 嗯 呀 哦 哈
       他 她 它 们 您 你 谁 哪 什 么 怎 样 吗 个 们 之 于 仍 已 将 曾 正 才 便 即 若 因
       the a an and or of to in is it for on with as at by be are was were this that
       from not but we they he she you i""".split()
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


@dataclass(frozen=True)
class Token:
    term: str
    start: int  # Unicode 码点偏移
    end: int


def tokenize(text: str, *, drop_stopwords: bool = True) -> list[Token]:
    tokens: list[Token] = []
    for m in _TOKEN_RE.finditer(text):
        raw = m.group(0)
        # 单个 CJK 字符原样；拉丁数字串小写（含点/下划线的标识符整体小写）
        term = raw if len(raw) == 1 and _is_cjk(raw) else raw.lower()
        if drop_stopwords and term in STOPWORDS:
            continue
        tokens.append(Token(term=term, start=m.start(), end=m.end()))
    return tokens


def token_terms(text: str) -> list[str]:
    return [t.term for t in tokenize(text)]
