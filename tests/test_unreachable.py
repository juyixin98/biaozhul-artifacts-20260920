"""不可达规则：被更高优先级规则完全遮蔽的规则须被报告。"""

import re

from app.kernel.diagnostics import unreachable_report
from app.kernel.lexer import Rule
from app.kernel.nfa import ast_to_nfa
from app.kernel.regex_ast import parse_pattern


def _run(spec, limits, logger):
    rules = [Rule(i, n, p, prio, skip=False) for i, (n, p, prio) in enumerate(spec)]
    nfas = [ast_to_nfa(parse_pattern(r.pattern, limits), tag=r.index, limits=limits) for r in rules]
    return rules, unreachable_report(nfas, rules, limits, logger)


def test_shadowed_keyword_is_unreachable(limits, logger):
    spec = [("IF", "if", 10), ("ALSO_IF", "if", 0)]
    _, report = _run(spec, limits, logger)
    by_name = {e.rule: e for e in report}
    assert by_name["ALSO_IF"].unreachable is True
    assert by_name["ALSO_IF"].witness is None
    assert by_name["IF"].unreachable is False


def test_identical_language_lower_priority_unreachable(limits, logger):
    spec = [("DIGITS", "[0-9]+", 5), ("INT", "[0-9]+", 0)]
    _, report = _run(spec, limits, logger)
    by_name = {e.rule: e for e in report}
    assert by_name["INT"].unreachable is True
    assert by_name["DIGITS"].unreachable is False


def test_partial_overlap_stays_reachable(limits, logger):
    spec = [("INT", "[0-9]+", 0), ("FLOAT", "[0-9]+\\.[0-9]+", 0)]
    rules, report = _run(spec, limits, logger)
    by_name = {e.rule: e for e in report}
    assert by_name["INT"].unreachable is False
    assert by_name["FLOAT"].unreachable is False
    # 可达见证须经 re 预言机复核：属于本规则且不属于任何击败者
    float_witness = by_name["FLOAT"].witness
    assert re.fullmatch(r"[0-9]+\.[0-9]+", float_witness)
    assert not re.fullmatch(r"[0-9]+", float_witness)


def test_unreachable_check_logged_with_reason(limits, logger):
    spec = [("IF", "if", 10), ("ALSO_IF", "if", 0)]
    _run(spec, limits, logger)
    entries = [e for e in logger.entries if e.event == "reachability_checked"]
    also = [e for e in entries if e.state["rule"] == "ALSO_IF"]
    assert also and also[0].state["unreachable"] is True
    assert also[0].state["beaten_by"] == ["IF"]
    assert "不可达" in also[0].rationale
