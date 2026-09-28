"""语料规范的语义校验（查询验证的第二道边界）。

结构校验（pydantic）之后、编译之前执行：
- 规则数/模式长度等资源上限
- 模式语法可解析（内核解析器复核）
- 能匹配空串的普通词元规则在此拒绝
"""

from __future__ import annotations

from app.config import Limits
from app.errors import InputError, ResourceExhausted
from app.kernel.lexer import Rule
from app.kernel.nfa import accepts_empty, ast_to_nfa
from app.kernel.regex_ast import parse_pattern
from app.runlog import RunLogger
from app.spec.models import LexerSpecIn

MODULE = "spec.validation"


def to_rules(spec: LexerSpecIn) -> list[Rule]:
    return [
        Rule(index=i, name=r.name, pattern=r.pattern, priority=r.priority, skip=r.skip)
        for i, r in enumerate(spec.rules)
    ]


def validate_spec(spec: LexerSpecIn, limits: Limits, logger: RunLogger) -> list[Rule]:
    if len(spec.rules) > limits.max_rules:
        raise ResourceExhausted(
            code="TOO_MANY_RULES",
            message=f"规则数 {len(spec.rules)} 超过上限 {limits.max_rules}",
            details={"rules": len(spec.rules), "limit": limits.max_rules},
        )
    rules = to_rules(spec)
    for rule in rules:
        ast = parse_pattern(rule.pattern, limits)  # 语法/资源错误原样上抛
        nfa = ast_to_nfa(ast, tag=rule.index, limits=limits)
        if accepts_empty(nfa):
            logger.log(
                MODULE,
                "rule_rejected",
                state={"rule": rule.name, "pattern": rule.pattern},
                rationale="规则语言包含空串，普通词元规则按契约拒绝（输入错误）",
                level="ERROR",
            )
            raise InputError(
                code="EMPTY_MATCH",
                message=f"规则 {rule.name!r} 能匹配空串，普通词元规则不允许",
                details={"rule": rule.name, "pattern": rule.pattern},
            )
    logger.log(
        MODULE,
        "spec_validated",
        state={"spec": spec.name, "version": spec.version, "rules": len(rules)},
        rationale="结构校验、语法解析、空串校验全部通过",
    )
    return rules
