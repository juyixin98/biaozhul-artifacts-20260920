"""模型层：把挖掘结果编译成 FST 并预组合查询管线。

每个已加载语料编译为一个 :class:`CompiledModel`：

* ``edit``  ：字符/词形编辑级联（由对齐统计挖掘，sub/ins/del/identity）；
* ``rules`` ：语料显式声明的词形规则级联；
* ``lexicon``：整串词典映射机（支持同输入多输出的歧义）；
* ``pipeline``：``edit ∘ rules ∘ lexicon`` 的预组合结果，查询时只需再与
  输入链接受机组合一次。

字母表取「词典键 + 规则符号 + 语料符号」之并集，保证恒等弧覆盖所有
应被原样放行的符号；未覆盖符号由查询验证层显式拒绝。

词级（``token_level=="word"``）的每个 FST 符号为「词 + :data:`WORD_BOUNDARY`
后缀」，使多字词与插入/删除能按词对齐，且枚举输出可无歧义切回。
"""

from __future__ import annotations

from dataclasses import dataclass

from wfst.algorithms.compose import compose
from wfst.builders import edit_cascade_fst, rewrite_rule_fst
from wfst.corpus.mining import EPSILON, MiningReport, Op
from wfst.corpus.spec import CorpusSpec
from wfst.core.fst import FST

WORD_BOUNDARY = "\x00"


@dataclass(slots=True)
class CompiledModel:
    corpus_id: str
    version: str
    token_level: str
    alphabet: frozenset[str]
    edit: FST
    rules: FST
    lexicon: FST
    pipeline: FST

    def describe(self) -> str:
        return (
            f"CompiledModel({self.corpus_id}@{self.version}, "
            f"level={self.token_level}, |alphabet|={len(self.alphabet)}, "
            f"pipeline={self.pipeline.describe()})"
        )


def _sym(token: str, word_level: bool) -> str:
    """原始词/字 -> FST 符号（词级加边界后缀）。"""
    return token + WORD_BOUNDARY if word_level else token


def _tokens(text: str, word_level: bool) -> list[str]:
    raw = text.split() if word_level else list(text)
    return [_sym(t, word_level) for t in raw]


def _collect_alphabet(
    spec: CorpusSpec, report: MiningReport, word_level: bool
) -> frozenset[str]:
    symbols: set[str] = set()
    for al in spec.alignments:
        symbols.update(_tokens(al.input, word_level))
        symbols.update(_tokens(al.output, word_level))
    for e in report.lexicon_entries:
        symbols.update(_tokens(e.text_in, word_level))
    for a in report.arcs:
        if a.ilabel != EPSILON:
            symbols.add(_sym(a.ilabel, word_level))
        if a.olabel != EPSILON:
            symbols.add(_sym(a.olabel, word_level))
    for r in spec.rules:
        symbols.add(_sym(r.ilabel, word_level))
        symbols.add(_sym(r.olabel, word_level))
    symbols.discard(EPSILON)
    return frozenset(symbols)


def compile_model(spec: CorpusSpec, report: MiningReport) -> CompiledModel:
    """把语料规范与挖掘报告编译为可查询模型。"""
    word_level = spec.token_level == "word"

    alphabet = _collect_alphabet(spec, report, word_level)

    subs: dict[tuple[str, str], float] = {}
    ins: dict[str, float] = {}
    dels: dict[str, float] = {}
    identity_weights: dict[str, float] = {}
    for arc in report.arcs:
        if arc.op is Op.SUB:
            subs[(_sym(arc.ilabel, word_level), _sym(arc.olabel, word_level))] = arc.cost
        elif arc.op is Op.INS:
            ins[_sym(arc.olabel, word_level)] = arc.cost
        elif arc.op is Op.DEL:
            dels[_sym(arc.ilabel, word_level)] = arc.cost
        elif arc.op is Op.IDENTITY:
            identity_weights[_sym(arc.ilabel, word_level)] = arc.cost
    edit = edit_cascade_fst(
        subs, ins, dels,
        alphabet=sorted(alphabet),
        name=f"edit:{spec.corpus_id}",
        identity_weights=identity_weights,
    )

    rule_triples = [
        (
            EPSILON if r.kind == "ins" else _sym(r.ilabel, word_level),
            EPSILON if r.kind == "del" else _sym(r.olabel, word_level),
            r.weight_hint,
        )
        for r in spec.rules
    ]
    rules = rewrite_rule_fst(
        rule_triples, alphabet=sorted(alphabet), name=f"rules:{spec.corpus_id}"
    )

    lex_spec = [(e.text_in, e.text_out, e.cost) for e in report.lexicon_entries]
    lex = _lexicon_fst(
        lex_spec, name=f"lex:{spec.corpus_id}", word_level=word_level
    )

    pipeline = compose(compose(edit, rules), lex)
    return CompiledModel(
        corpus_id=spec.corpus_id,
        version=spec.version,
        token_level=spec.token_level,
        alphabet=alphabet,
        edit=edit,
        rules=rules,
        lexicon=lex,
        pipeline=pipeline,
    )


def _lexicon_fst(
    entries: list[tuple[str, str, float]],
    name: str,
    word_level: bool = False,
) -> FST:
    """词典机：逐符号展开，长度不等时短侧补 epsilon 弧。

    条目权重全部放在终态（路径弧零代价），同一 (in,out) 对的终态权重
    取最小值。
    """
    fst = FST(name=name)
    for text_in, text_out, weight in entries:
        li, lo = _tokens(text_in, word_level), _tokens(text_out, word_level)
        n = max(len(li), len(lo))
        li = li + [EPSILON] * (n - len(li))
        lo = lo + [EPSILON] * (n - len(lo))
        s = 0
        for a, b in zip(li, lo):
            nxt = fst.new_state()
            fst.add_arc(s, nxt, a, b, 0.0)
            s = nxt
        prev = fst.finals.get(s)
        fst.set_final(s, weight if prev is None else min(prev, weight))
    return fst
