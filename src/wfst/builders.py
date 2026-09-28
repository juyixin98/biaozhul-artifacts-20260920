"""FST 构建器：把词典、对齐规则、字符纠错级联编译成 FST。

所有构建器产出 :class:`~wfst.core.fst.FST`，供组合层使用。
代价为可加的对数域风格权重（越小越优）；调用方自行定义尺度。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from wfst.core.fst import EPSILON, FST


def chain_acceptor(tokens: Sequence[str], name: str = "input") -> FST:
    """把固定输入串编译成线性链接受机（ilabel==olabel）。"""
    fst = FST(name=name)
    for i, tok in enumerate(tokens):
        fst.add_arc(i, i + 1, tok, tok, 0.0)
    fst.set_final(len(tokens), 0.0)
    return fst


def lexicon_fst(
    entries: Mapping[str, Mapping[str, float] | Sequence[float] | float]
    | Sequence[tuple[str, str, float]],
    name: str = "lexicon",
) -> FST:
    """词典映射机：整串输入 -> 一个或多个整串输出。

    支持三种条目形式：
      * 序列形式 ``[(in, out, weight), ...]``（允许同输入多输出、重复输出）；
      * 映射 ``{in: {out: weight}}``（多输出）；
      * 映射 ``{in: weight}`` 仅给出权重时输出等于输入（恒等词典）。

    每个条目是一条独立路径：从初态逐字符展开，末态为终态。多条目共享
    前缀时合并相同的前缀弧（同端同标 tropical 合并由组合层负责，此处
    路径独立即可，最短路径层按输出串去重）。
    """
    fst = FST(name=name)

    def add_path(src: int, labels_in: Sequence[str], labels_out: Sequence[str],
                 weight: float) -> None:
        # 简单实现：每条目独立路径（合成夹具规模小，清晰优先）。
        s = src
        pairs = list(zip(labels_in, labels_out))
        for il, ol in pairs:
            nxt = fst.new_state()
            fst.add_arc(s, nxt, il, ol, 0.0)
            s = nxt
        fst.set_final(s, fst.finals.get(s, 0.0) + weight)

    if isinstance(entries, Mapping):
        for text_in, spec in entries.items():
            if isinstance(spec, Mapping):
                for text_out, w in spec.items():
                    add_path(0, tuple(text_in), tuple(text_out), float(w))
            else:
                add_path(0, tuple(text_in), tuple(text_in), float(spec))
    else:
        for text_in, text_out, w in entries:
            add_path(0, tuple(text_in), tuple(text_out), float(w))
    return fst


def edit_cascade_fst(
    substitutions: Mapping[tuple[str, str], float],
    insertions: Mapping[str, float],
    deletions: Mapping[str, float],
    alphabet: Sequence[str] | None = None,
    name: str = "edit",
    identity_weight: float = 0.0,
    identity_weights: Mapping[str, float] | None = None,
) -> FST:
    """字符级编辑级联（单状态机）：替换 / 插入 / 删除 / 恒等。

    * 替换 ``a -> b``：弧 ilabel=a, olabel=b；
    * 插入 ``b``：弧 ilabel=ε, olabel=b（不消耗输入，产出 b）；
    * 删除 ``a``：弧 ilabel=a, olabel=ε（消耗 a，不产出）；
    * 恒等：字母表中每个字符 a -> a；代价取 ``identity_weights[a]``
      （挖掘估计），未提供时用统一 ``identity_weight``（默认 0）。

    所有操作自环于唯一初/终态，因此可作用于输入任意位置、任意次数。
    """
    identity_weights = identity_weights or {}
    fst = FST(name=name)
    for (a, b), w in substitutions.items():
        fst.add_arc(0, 0, a, b, float(w))
    for b, w in insertions.items():
        fst.add_arc(0, 0, EPSILON, b, float(w))
    for a, w in deletions.items():
        fst.add_arc(0, 0, a, EPSILON, float(w))
    for a in alphabet or ():
        fst.add_arc(
            0, 0, a, a, float(identity_weights.get(a, identity_weight))
        )
    fst.set_final(0, 0.0)
    return fst


def rewrite_rule_fst(
    rules: Sequence[tuple[str, str, float]],
    alphabet: Sequence[str] | None = None,
    name: str = "rules",
    identity_weight: float = 0.0,
) -> FST:
    """词形/规整规则级联：``(input_token, output_token, weight)`` 的单状态机。

    与字符级联同构，但符号粒度为词/形位；提供 alphabet 时同时给出恒等弧。
    """
    fst = FST(name=name)
    for a, b, w in rules:
        fst.add_arc(0, 0, a, b, float(w))
    for a in alphabet or ():
        fst.add_arc(0, 0, a, a, identity_weight)
    fst.set_final(0, 0.0)
    return fst
