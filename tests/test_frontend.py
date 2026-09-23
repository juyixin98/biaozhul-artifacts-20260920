"""词法/语法前端测试：位置保留、运算符优先级、错误诊断。"""

import unittest

from slang.errors import CompileError
from slang.lexer import TokenType, tokenize
from slang.location import SourceText
from slang.parser import parse_source


class TestLexer(unittest.TestCase):
    def test_tokens_and_span(self):
        src = SourceText("var x = 1;")
        toks = tokenize(src)
        kinds = [t.type for t in toks]
        self.assertEqual(kinds, [
            TokenType.VAR, TokenType.IDENT, TokenType.ASSIGN,
            TokenType.INT, TokenType.SEMI, TokenType.EOF,
        ])
        # 位置：'1' 在偏移 8，第 1 行第 9 列
        one = toks[3]
        self.assertEqual(one.value, "1")
        self.assertEqual((one.span.line, one.span.col), (1, 9))

    def test_line_col_after_newline(self):
        src = SourceText("a\n  b")
        toks = tokenize(src)
        # a, b, EOF
        self.assertEqual((toks[1].span.line, toks[1].span.col), (2, 3))

    def test_comments(self):
        src = SourceText("// line\nx; /* block */ y;")
        toks = [t for t in tokenize(src) if t.type is not TokenType.EOF]
        names = [t.value for t in toks if t.type is TokenType.IDENT]
        self.assertEqual(names, ["x", "y"])

    def test_bad_char(self):
        with self.assertRaises(CompileError):
            tokenize(SourceText("var @ = 1;"))

    def test_unterminated_block_comment(self):
        with self.assertRaises(CompileError):
            tokenize(SourceText("/* never ends"))


class TestParser(unittest.TestCase):
    def _parse(self, text):
        return parse_source(SourceText(text))

    def test_minimal_program(self):
        p = self._parse("fn main() { }")
        self.assertEqual(len(p.funcs), 1)
        self.assertEqual(p.funcs[0].name, "main")

    def test_precedence(self):
        from slang import ast_nodes as ast
        p = self._parse("fn f(): int { return 1 + 2 * 3; }")
        ret = p.funcs[0].body.stmts[0]
        self.assertIsInstance(ret, ast.ReturnStmt)
        self.assertIsInstance(ret.value, ast.Binary)
        self.assertEqual(ret.value.op, "+")
        # 右侧必须是乘法
        self.assertIsInstance(ret.value.right, ast.Binary)
        self.assertEqual(ret.value.right.op, "*")

    def test_unary_and_parens(self):
        from slang import ast_nodes as ast
        p = self._parse("fn f(): int { return -(2 + 3); }")
        ret = p.funcs[0].body.stmts[0]
        self.assertIsInstance(ret.value, ast.Unary)
        self.assertEqual(ret.value.op, "-")
        self.assertIsInstance(ret.value.operand, ast.Binary)

    def test_comparison_and_logical(self):
        from slang import ast_nodes as ast
        p = self._parse("fn f(): bool { return a && b || c; }")
        e = p.funcs[0].body.stmts[0].value
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "||")   # || 最弱，在最外

    def test_positions_kept(self):
        p = self._parse("fn f(int x): int {\n  return x;\n}")
        ret = p.funcs[0].body.stmts[0]
        self.assertEqual(ret.span.line, 2)

    def test_missing_semicolon(self):
        with self.assertRaises(CompileError):
            self._parse("fn f() { print 1 }")

    def test_bad_type(self):
        with self.assertRaises(CompileError):
            self._parse("fn f(string x) { }")

    def test_dup_param(self):
        with self.assertRaises(CompileError):
            self._parse("fn f(int x, int x) { }")


if __name__ == "__main__":
    unittest.main()
