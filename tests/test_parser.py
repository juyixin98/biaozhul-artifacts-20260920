"""词法/语法：源码位置保留、优先级、作用域与错误恢复。"""

import unittest

from ssa_tool.errors import LexError, ParseError
from ssa_tool.lexer import tokenize
from ssa_tool.parser import parse


SRC = """\
// 注释
func main(a) {
    var x = a + 2 * 3;   // 行 3
    var y = (a + 2) * 3;
    if (x < 10 && y > 5) {
        return -x;
    } else {
        x = !a;
    }
    return x;
}
"""


class TestLexer(unittest.TestCase):
    def test_token_positions(self):
        toks = tokenize("var x = 10;")
        # var(0) x(4) =(6) 10(8) ;(10)
        self.assertEqual([(t.kind, t.value, t.offset) for t in toks[:5]],
                         [("KEYWORD", "var", 0),
                          ("IDENT", "x", 4),
                          ("OP", "=", 6),
                          ("INT", "10", 8),
                          ("OP", ";", 10)])

    def test_line_tracking(self):
        toks = tokenize("a\n\n b")
        self.assertEqual([(t.value, t.line) for t in toks if t.kind != "EOF"],
                         [("a", 1), ("b", 3)])

    def test_line_comment(self):
        toks = tokenize("a // ignored\nb")
        vals = [t.value for t in toks if t.kind != "EOF"]
        self.assertEqual(vals, ["a", "b"])

    def test_block_comment(self):
        vals = [t.value for t in tokenize("a /* x \n y */ b")
                if t.kind != "EOF"]
        self.assertEqual(vals, ["a", "b"])

    def test_unterminated_comment(self):
        with self.assertRaises(LexError):
            tokenize("a /* never closed")

    def test_bad_char(self):
        with self.assertRaises(LexError):
            tokenize("@")


class TestParser(unittest.TestCase):
    def test_parses(self):
        p = parse(SRC, "t.toy")
        self.assertEqual(len(p.functions), 1)
        self.assertEqual(p.functions[0].name, "main")

    def test_span_roundtrip(self):
        # 表达式 x < 10 的 span 必须精确切回源码子串
        p = parse(SRC)
        if_node = p.functions[0].body[2]
        text = SRC[if_node.cond.span.start_off:if_node.cond.span.end_off]
        self.assertEqual(text, "x < 10 && y > 5")
        # 赋值 x = !a 中 !a 的 span
        assign = if_node.else_body[0]
        self.assertEqual(SRC[assign.value.span.start_off:
                             assign.value.span.end_off], "!a")
        # 行号正确
        self.assertEqual(if_node.cond.span.start_line, 5)

    def test_precedence(self):
        from ssa_tool import ast_nodes as ast
        p = parse("func main(){ var x = 1 + 2 * 3; }")
        e = p.functions[0].body[0].init
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.Binary)
        self.assertEqual(e.right.op, "*")

    def test_unary_and_comparison(self):
        p = parse("func main(){ var x = -1 < 2; }")
        e = p.functions[0].body[0].init
        self.assertEqual(e.op, "<")
        self.assertEqual(e.left.op, "-")

    def test_duplicate_decl(self):
        with self.assertRaises(ParseError):
            parse("func main(){ var x; var x; }")

    def test_undeclared_use(self):
        with self.assertRaises(ParseError):
            parse("func main(){ y = 1; }")

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError):
            parse("func main(){ var x = 1 }")

    def test_main_only(self):
        with self.assertRaises(ParseError):
            parse("func f(){}")

    def test_if_else_if_chain(self):
        p = parse("func main(a){ if(a){} else if(a){} else {} }")
        self.assertIsNotNone(p.functions[0].body[0].else_body)


if __name__ == "__main__":
    unittest.main()
