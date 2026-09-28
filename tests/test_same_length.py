"""同长匹配决胜：显式优先级优先，其次声明顺序。"""

from app.kernel.lexer import CompiledLexer

from conftest import make_rules


def test_higher_priority_wins_same_length(limits, logger):
    # 两规则都恰匹配 "abc"：优先级 5 的 B 胜
    lexer = CompiledLexer.build(
        make_rules([("A", "abc", 0), ("B", "[a]bc", 5)]), limits, logger
    )
    tokens = lexer.tokenize("abc", limits, logger)
    assert [(t.type, t.text) for t in tokens] == [("B", "abc")]


def test_declaration_order_breaks_priority_tie(limits, logger):
    # 同优先级同长：声明在前的 A 胜
    lexer = CompiledLexer.build(
        make_rules([("A", "abc", 0), ("B", "[a]bc", 0)]), limits, logger
    )
    tokens = lexer.tokenize("abc", limits, logger)
    assert [(t.type, t.text) for t in tokens] == [("A", "abc")]


def test_longer_match_beats_higher_priority(limits, logger):
    # 优先级 10 的短规则 vs 优先级 0 的长规则：最长匹配优先
    lexer = CompiledLexer.build(
        make_rules([("SHORT", "ab", 10), ("LONG", "abcd", 0)]), limits, logger
    )
    tokens = lexer.tokenize("abcd", limits, logger)
    assert [(t.type, t.text) for t in tokens] == [("LONG", "abcd")]
