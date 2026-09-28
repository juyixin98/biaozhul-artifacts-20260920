"""查询验证与执行层。

职责：
* 在系统边界校验输入（空输入、超长、字母表外符号、非法 k/预算），
  失败给出**明确类别**（:class:`ErrorCategory`），绝不返回成功；
* 固定输入为链接受机，与预组合管线组合后做 n-最短枚举；
* 提供一个**分阶段顺序执行**的参考实现
  （:func:`transduce_stagewise`）：edit、rules、lexicon 逐级组合并枚举，
  与一次性大组合 :func:`run_query` 走不同计算路径，相互交叉核验。

词级符号约定：word 级模型在编译期给每个符号追加 ``\\x00`` 边界后缀，
链接受机、编辑级联、规则、词典全部使用同一后缀符号，因此枚举输出是
``"词1\\x00词2\\x00"`` 形式，可无歧义切回词序列。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from wfst.algorithms.compose import compose
from wfst.algorithms.shortest_paths import Hypothesis, nbest_paths
from wfst.builders import chain_acceptor
from wfst.core.fst import AlignmentError, FST, NegativeCycleError
from wfst.index.registry import WORD_BOUNDARY, CompiledModel


class ErrorCategory(str, Enum):
    MODEL_NOT_FOUND = "model_not_found"
    EMPTY_INPUT = "empty_input"
    INPUT_TOO_LONG = "input_too_long"
    UNKNOWN_SYMBOL = "unknown_symbol"
    INVALID_PARAMETER = "invalid_parameter"
    UNACCEPTABLE_INPUT = "unacceptable_input"
    NEGATIVE_CYCLE = "negative_cycle"


class QueryError(ValueError):
    """边界校验/执行失败；``category`` 供 API 映射为确定的状态码。"""

    def __init__(self, category: ErrorCategory, message: str,
                 details: dict | None = None):
        self.category = category
        self.details = details or {}
        super().__init__(message)


@dataclass(slots=True)
class QueryResponse:
    model_id: str
    version: str
    token_level: str
    input_tokens: list[str]
    hypotheses: list[Hypothesis]
    complete: bool
    pops: int
    budget: int
    mode: str = "composed"
    trace: list[str] = field(default_factory=list)

    def output_texts(self) -> list[str]:
        """把假设的符号串还原为人类可读文本（词级去后缀并空格连接）。"""
        return [render_output(h.output, self.token_level) for h in self.hypotheses]


def render_output(symbols: str, token_level: str) -> str:
    if token_level == "char":
        return symbols
    # 形如 "词1\x00词2\x00"；按边界切分后非空片段即词（后缀已被切分移除）。
    return " ".join(part for part in symbols.split(WORD_BOUNDARY) if part)


def tokenize_for_model(text: str, model: CompiledModel) -> list[str]:
    """按模型粒度把文本切成 FST 符号（词级加边界后缀）。"""
    if model.token_level == "char":
        return list(text)
    return [w + WORD_BOUNDARY for w in text.split()]


def validate_tokens(tokens: list[str], model: CompiledModel,
                    max_len: int) -> list[str]:
    if not tokens:
        raise QueryError(ErrorCategory.EMPTY_INPUT, "输入为空（至少需要一个符号）")
    if len(tokens) > max_len:
        raise QueryError(
            ErrorCategory.INPUT_TOO_LONG,
            f"输入长度 {len(tokens)} 超过上限 {max_len}",
            {"length": len(tokens), "max": max_len},
        )
    unknown = sorted({t for t in tokens if t not in model.alphabet})
    if unknown:
        raise QueryError(
            ErrorCategory.UNKNOWN_SYMBOL,
            f"存在模型字母表外的符号：{unknown}",
            {"unknown": unknown},
        )
    return tokens


def _nbest_or_raise(composed: FST, k: int, budget: int, stage: int | None):
    try:
        return nbest_paths(composed, k=k, budget=budget)
    except NegativeCycleError as exc:
        raise QueryError(
            ErrorCategory.NEGATIVE_CYCLE,
            str(exc),
            {"cycle_states": list(exc.cycle_states), "stage": stage},
        ) from exc
    except AlignmentError as exc:
        raise QueryError(
            ErrorCategory.UNACCEPTABLE_INPUT,
            str(exc) if stage is None else f"第 {stage} 级无接受路径：{exc}",
            {"stage": stage},
        ) from exc


def run_query(
    model: CompiledModel,
    text: str,
    k: int = 5,
    budget: int = 200_000,
    max_input_len: int = 64,
) -> QueryResponse:
    """对输入文本执行一次性组合的 n-最短查询。"""
    if k <= 0:
        raise QueryError(ErrorCategory.INVALID_PARAMETER, "k 必须为正整数")
    if budget <= 0:
        raise QueryError(ErrorCategory.INVALID_PARAMETER, "budget 必须为正整数")

    tokens = tokenize_for_model(text, model)
    validate_tokens(tokens, model, max_input_len)

    chain = chain_acceptor(tokens, name="input")
    trace = [
        f"输入符号序列={tokens}",
        f"与预组合管线 {model.pipeline.name} 组合"
        f"（管线 {model.pipeline.num_states} 状态）",
    ]
    composed = compose(chain, model.pipeline, name="query")
    trace.append(f"组合结果：{composed.num_states} 状态 / {len(composed.arcs)} 弧")
    result = _nbest_or_raise(composed, k, budget, None)
    trace.append(
        f"best-first 弹出 {result.pops} 次（预算 {budget}），"
        f"完备={result.complete}，候选 {len(result.hypotheses)} 条；"
        f"判定依据：代价升序，平手按输出符号字典序"
    )
    return QueryResponse(
        model_id=model.corpus_id,
        version=model.version,
        token_level=model.token_level,
        input_tokens=tokens,
        hypotheses=result.hypotheses,
        complete=result.complete,
        pops=result.pops,
        budget=budget,
        mode="composed",
        trace=trace,
    )


def transduce_stagewise(
    model: CompiledModel,
    text: str,
    k: int = 5,
    budget: int = 200_000,
    max_input_len: int = 64,
    beam_width: int = 200,
) -> QueryResponse:
    """分阶段顺序执行参考：逐级（组合 → n-最短枚举 → 重建链）直到词典。

    与 :func:`run_query` 的一次性预组合走**不同的计算路径**（中间显式
    物化为链接受机），用于交叉验证组合结果与顺序执行一致。每级把不同
    中间输出按累计代价保留至多 ``beam_width`` 条（束宽），最终再截到
    ``k`` 条。插入自环理论上可产生无穷中间串，故束宽是必要的终止保障；
    对随附的小型穷举夹具束宽不会成为瓶颈（测试显式断言这一点）。
    """
    if k <= 0:
        raise QueryError(ErrorCategory.INVALID_PARAMETER, "k 必须为正整数")
    if budget <= 0:
        raise QueryError(ErrorCategory.INVALID_PARAMETER, "budget 必须为正整数")
    if beam_width < k:
        raise QueryError(
            ErrorCategory.INVALID_PARAMETER, "beam_width 不能小于 k"
        )
    tokens = tokenize_for_model(text, model)
    validate_tokens(tokens, model, max_input_len)

    stage_fsts = (model.edit, model.rules, model.lexicon)
    # (中间符号串, 累计代价)；初始为唯一输入。
    partials: list[tuple[list[str], float]] = [(tokens, 0.0)]
    trace: list[str] = []

    for depth, stage in enumerate(stage_fsts):
        best: dict[str, float] = {}
        dead_branches = 0
        for mid_tokens, base_cost in partials:
            chain = chain_acceptor(mid_tokens, name=f"mid{depth}")
            composed = compose(chain, stage, name=f"stage{depth}")
            try:
                result = nbest_paths(composed, k=beam_width, budget=budget)
            except NegativeCycleError as exc:
                raise QueryError(
                    ErrorCategory.NEGATIVE_CYCLE,
                    str(exc),
                    {"cycle_states": list(exc.cycle_states), "stage": depth},
                ) from exc
            except AlignmentError:
                # 该中间串在本级无接受路径（例如编辑产物不是任何词典键）：
                # 这是正常的分支消亡，跳过；仅当所有分支都消亡才整体判失败。
                dead_branches += 1
                continue
            for hyp in result.hypotheses:
                total = base_cost + hyp.cost
                if hyp.output not in best or total < best[hyp.output]:
                    best[hyp.output] = total
        if not best:
            raise QueryError(
                ErrorCategory.UNACCEPTABLE_INPUT,
                f"第 {depth} 级（{stage.name}）上所有 {len(partials)} 个中间分支"
                "均无接受路径，输入不被接受",
                {"stage": depth, "dead_branches": dead_branches},
            )
        kept = sorted(best.items(), key=lambda kv: (kv[1], kv[0]))[:beam_width]
        beam_bound = len(best) > beam_width
        partials = [(_symbols_of(out, model.token_level), c) for out, c in kept]
        trace.append(
            f"级 {depth}（{stage.name}）后保留 {len(partials)} 条中间结果"
            f"（消亡分支 {dead_branches} 条"
            f"{'，束宽截断！' if beam_bound else ''}）："
            + "; ".join(
                f"{render_output(out, model.token_level)}={c:.4f}"
                for out, c in kept[: min(len(kept), 8)]
            )
        )

    ranked = sorted(partials, key=lambda x: (x[1], "".join(x[0])))[:k]
    hyps = []
    for tokens_out, cost in ranked:
        joined = (
            "".join(tokens_out)
            if model.token_level == "char"
            else WORD_BOUNDARY.join(tokens_out)
        )
        hyps.append(Hypothesis(output=joined, cost=cost))
    return QueryResponse(
        model_id=model.corpus_id,
        version=model.version,
        token_level=model.token_level,
        input_tokens=tokens,
        hypotheses=hyps,
        complete=True,
        pops=0,
        budget=budget,
        mode="stagewise",
        trace=trace,
    )


def _symbols_of(concatenated: str, token_level: str) -> list[str]:
    """把 nbest 的拼接输出还原为符号列表（词级按边界切）。"""
    if token_level == "char":
        return list(concatenated)
    # 拼接形如 "w1\x00w2\x00"；保留每个词的边界后缀。
    return [p + WORD_BOUNDARY for p in concatenated.split(WORD_BOUNDARY) if p]
