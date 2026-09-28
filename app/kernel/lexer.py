"""词法器：规则编译（解析 -> 空串校验 -> NFA -> DFA）与最长匹配运行时。

匹配语义（固定契约）：
1. 最长匹配优先（max-munch）
2. 等长时显式优先级 priority 大者胜
3. 仍并列时声明顺序靠前者胜
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import Limits
from app.errors import InputError, ResourceExhausted
from app.kernel.dfa import DFA, nfa_to_dfa
from app.kernel.nfa import NFA, accepts_empty, ast_to_nfa, combine_nfas
from app.kernel.regex_ast import parse_pattern
from app.runlog import RunLogger

MODULE = "kernel.lexer"


@dataclass(frozen=True)
class Rule:
    index: int
    name: str
    pattern: str
    priority: int
    skip: bool


@dataclass(frozen=True)
class Token:
    type: str
    start: int
    end: int
    text: str

    def to_dict(self) -> dict:
        return {"type": self.type, "start": self.start, "end": self.end, "text": self.text}


def rule_winner(accept_tags: frozenset[int], rules: list[Rule]) -> int | None:
    """等长并列时的胜者：优先级大者，其次声明顺序靠前者。"""
    best: int | None = None
    for tag in accept_tags:
        if tag < 0 or tag >= len(rules):
            continue
        if best is None:
            best = tag
            continue
        r, b = rules[tag], rules[best]
        if (r.priority, -r.index) > (b.priority, -b.index):
            best = tag
    return best


class CompiledLexer:
    def __init__(self, rules: list[Rule], dfa: DFA):
        self.rules = rules
        self.dfa = dfa
        # 每个 DFA 状态预计算胜者规则
        self._winner: list[int | None] = [rule_winner(tags, rules) for tags in dfa.accepts]

    # ---- 编译 ----
    @staticmethod
    def build(rules: list[Rule], limits: Limits, logger: RunLogger) -> "CompiledLexer":
        nfas: list[NFA] = []
        for rule in rules:
            ast = parse_pattern(rule.pattern, limits)
            nfa = ast_to_nfa(ast, tag=rule.index, limits=limits)
            if accepts_empty(nfa):
                logger.log(
                    MODULE,
                    "rule_rejected",
                    state={"rule": rule.name, "pattern": rule.pattern},
                    rationale="规则语言包含空串：起始态 ε 闭包可达接受态，"
                    "空匹配词元会导致词法循环无法推进，按契约拒绝",
                    level="ERROR",
                )
                raise InputError(
                    code="EMPTY_MATCH",
                    message=f"规则 {rule.name!r} 能匹配空串，普通词元规则不允许",
                    details={"rule": rule.name, "pattern": rule.pattern},
                )
            nfas.append(nfa)
            logger.log(
                MODULE,
                "rule_compiled",
                state={"rule": rule.name, "nfa_states": nfa.nstates},
                rationale="正则解析为 AST 并经 Thompson 构造为 NFA；空串校验通过",
            )
        combined = combine_nfas(nfas, limits)
        dfa = nfa_to_dfa(combined, limits)
        logger.log(
            MODULE,
            "dfa_built",
            state={
                "nfa_states": combined.nstates,
                "dfa_states": dfa.nstates,
                "rules": len(rules),
            },
            rationale="子集构造完成；按字母表划分单元转移，接受态携带规则标签集",
        )
        return CompiledLexer(rules, dfa)

    # ---- 运行时 ----
    def tokenize(self, text: str, limits: Limits, logger: RunLogger) -> list[Token]:
        if len(text) > limits.max_input_chars:
            raise ResourceExhausted(
                code="INPUT_TOO_LONG",
                message=f"输入长度 {len(text)} 超过上限 {limits.max_input_chars}",
                details={"length": len(text), "limit": limits.max_input_chars},
            )
        tokens: list[Token] = []
        pos = 0
        n = len(text)
        while pos < n:
            state = self.dfa.start
            last_accept_pos: int | None = None
            last_accept_rule: int | None = None
            i = pos
            while i < n:
                nxt = self.dfa.find_edge(state, ord(text[i]))
                if nxt is None:
                    break
                state = nxt
                i += 1
                winner = self._winner[state]
                if winner is not None:
                    last_accept_pos = i
                    last_accept_rule = winner
            if last_accept_pos is None:
                logger.log(
                    MODULE,
                    "lex_failed",
                    state={"offset": pos, "char": text[pos], "codepoint": ord(text[pos])},
                    rationale="从当前偏移无任何规则可匹配至少一个字符，最长匹配失败",
                    level="ERROR",
                )
                raise InputError(
                    code="LEX_NO_MATCH",
                    message=f"偏移 {pos} 处无法匹配字符 {text[pos]!r}",
                    details={"offset": pos, "char": text[pos], "codepoint": ord(text[pos])},
                )
            rule = self.rules[last_accept_rule]  # type: ignore[index]
            if not rule.skip:
                tokens.append(Token(rule.name, pos, last_accept_pos, text[pos:last_accept_pos]))
            pos = last_accept_pos
        logger.log(
            MODULE,
            "lex_done",
            state={"input_chars": n, "tokens": len(tokens)},
            rationale="最长匹配优先，等长按显式优先级与声明顺序决出胜者",
        )
        return tokens

    # ---- 序列化（索引与模型层持久化）----
    def to_dict(self) -> dict:
        return {
            "rules": [
                {
                    "index": r.index,
                    "name": r.name,
                    "pattern": r.pattern,
                    "priority": r.priority,
                    "skip": r.skip,
                }
                for r in self.rules
            ],
            "dfa": self.dfa.to_dict(),
        }

    @staticmethod
    def from_dict(d: dict) -> "CompiledLexer":
        rules = [Rule(**r) for r in d["rules"]]
        return CompiledLexer(rules, DFA.from_dict(d["dfa"]))
