"""词法/语法分析器测试。"""

import unittest

from byteverifier.common import ToolError
from byteverifier.parser import parse_source


class TestLexer(unittest.TestCase):
    def test_tokens_with_positions(self):
        from byteverifier.lexer import Lexer
        toks = Lexer("int x = 12;", "t").tokenize()
        kinds = [(t.kind, t.value) for t in toks]
        self.assertEqual(kinds[0], ("KEYWORD", "int"))
        self.assertEqual(toks[0].span.line, 1)
        self.assertEqual(toks[-1].kind, "EOF")

    def test_comments(self):
        from byteverifier.lexer import Lexer
        toks = Lexer("// 行注释\nint /* 块\n注释 */ x;", "t").tokenize()
        self.assertEqual(toks[0].value, "int")
        self.assertEqual(toks[0].span.line, 2)

    def test_bad_char(self):
        from byteverifier.lexer import Lexer
        with self.assertRaises(ToolError) as cm:
            Lexer("@", "t").tokenize()
        self.assertEqual(cm.exception.kind, "unexpected.char")


class TestParser(unittest.TestCase):
    def test_minimal(self):
        funcs = parse_source("void main() { }")
        self.assertEqual(len(funcs), 1)
        self.assertEqual(funcs[0].name, "main")

    def test_precedence(self):
        from byteverifier import ast_nodes as ast
        funcs = parse_source("int f() { return 1 + 2 * 3; }")
        ret = funcs[0].body[0]
        self.assertIsInstance(ret.value, ast.Binary)
        self.assertEqual(ret.value.op, "+")
        self.assertIsInstance(ret.value.right, ast.Binary)
        self.assertEqual(ret.value.right.op, "*")

    def test_unary_and_paren(self):
        from byteverifier import ast_nodes as ast
        funcs = parse_source("int f() { return -(1 + 2); }")
        u = funcs[0].body[0].value
        self.assertIsInstance(u, ast.Unary)
        self.assertEqual(u.op, "-")

    def test_else_if_chain(self):
        from byteverifier import ast_nodes as ast
        funcs = parse_source(
            "void f(bool a) { if (a) { } else if (a) { } else { } }"
        )
        ifs = funcs[0].body[0]
        self.assertIsInstance(ifs, ast.IfStmt)
        self.assertEqual(len(ifs.else_body), 1)
        self.assertIsInstance(ifs.else_body[0], ast.IfStmt)

    def test_errors(self):
        bad_sources = [
            "",                       # 空程序
            "void f() {",             # 缺右括号
            "void f(1) { }",          # 参数类型错误
            "void f(int x, int x) { }",  # 重名参数
            "void f() { int ; }",     # 缺变量名
            "void f() { 123; }",      # 非法语句
            "int f() { return 1 }",   # 缺分号
            "void f() { f(1 2); }",   # 实参间缺逗号
        ]
        for src in bad_sources:
            with self.subTest(src=src):
                with self.assertRaises(ToolError):
                    parse_source(src)


if __name__ == "__main__":
    unittest.main()
