"""空串匹配规则拒绝：能匹配空串的普通词元规则是输入错误。"""

import pytest

from app.errors import ErrorCategory, InputError
from app.kernel.lexer import CompiledLexer

from conftest import make_rules


@pytest.mark.parametrize("pattern", ["a*", "(a|)", "x?", "a{0,2}", "(ab)*", "|a"])
def test_empty_matching_rule_rejected(limits, logger, pattern):
    with pytest.raises(InputError) as exc:
        CompiledLexer.build(make_rules([("BAD", pattern)]), limits, logger)
    assert exc.value.category is ErrorCategory.INPUT_ERROR
    assert exc.value.code == "EMPTY_MATCH"
    assert exc.value.details["rule"] == "BAD"


def test_rejection_is_logged_with_rationale(limits, logger):
    with pytest.raises(InputError):
        CompiledLexer.build(make_rules([("BAD", "a*")]), limits, logger)
    errors = [e for e in logger.entries if e.event == "rule_rejected"]
    assert errors and errors[0].level == "ERROR"
    assert "空串" in errors[0].rationale
    assert errors[0].run_id == logger.run_id


def test_nonempty_rules_accepted(limits, logger):
    lexer = CompiledLexer.build(make_rules([("A", "a+"), ("B", "b")]), limits, logger)
    assert lexer.dfa.nstates > 0
