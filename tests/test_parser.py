"""词法/语法分析测试。"""
from __future__ import annotations

import unittest

from tinyinfer.errors import LexError, ParseError
from tinyinfer.lexer import Lexer
from tinyinfer.pipeline import parse_source


class LexerTests(unittest.TestCase):
    def test_basic_tokens(self) -> None:
        toks = Lexer("let x = 1 + 2").tokenize()
        kinds = [t.kind for t in toks]
        self.assertEqual(kinds, ["let", "IDENT", "=", "INT", "+", "INT"])
        self.assertEqual(toks[1].value, "x")
        self.assertEqual(toks[3].span.start.line, 1)

    def test_positions_multiline(self) -> None:
        toks = Lexer("a\n  b").tokenize()
        self.assertEqual(toks[1].span.start.line, 2)
        self.assertEqual(toks[1].span.start.column, 3)

    def test_comments(self) -> None:
        toks = Lexer("x // line comment\n (* block (* nested *) *) y").tokenize()
        self.assertEqual([t.kind for t in toks], ["IDENT", "IDENT"])

    def test_unterminated_block_comment(self) -> None:
        with self.assertRaises(LexError):
            Lexer("(* never ends").tokenize()

    def test_bad_character(self) -> None:
        with self.assertRaises(LexError):
            Lexer("let $ = 1").tokenize()

    def test_number_glued_to_ident(self) -> None:
        with self.assertRaises(LexError):
            Lexer("123abc").tokenize()

    def test_two_char_operators(self) -> None:
        toks = Lexer("a <= b == c <- d").tokenize()
        self.assertEqual([t.value for t in toks],
                         ["a", "<=", "b", "==", "c", "<-", "d"])


class ParserTests(unittest.TestCase):
    def test_empty_program_rejected(self) -> None:
        with self.assertRaises(ParseError):
            parse_source("")
        with self.assertRaises(ParseError):
            parse_source("(* only comment *)")

    def test_simple_expression(self) -> None:
        prog = parse_source("1 + 2")
        self.assertEqual(len(prog.bindings), 0)
        self.assertIsNotNone(prog.final_expr)

    def test_top_level_lets_lowered(self) -> None:
        from tinyinfer.inference import lower_program
        prog = parse_source("let a = 1\nlet b = 2\na + b")
        self.assertEqual(len(prog.bindings), 2)
        lowered = lower_program(prog)
        self.assertIsNotNone(lowered)

    def test_curried_param_sugar(self) -> None:
        from tinyinfer import ast
        prog = parse_source("let f x y = x + y\nf 1 2")
        bound = prog.bindings[0].bound
        self.assertIsInstance(bound, ast.Fun)
        self.assertEqual(bound.param, "x")
        self.assertIsInstance(bound.body, ast.Fun)
        self.assertEqual(bound.body.param, "y")

    def test_precedence(self) -> None:
        from tinyinfer import ast
        # 1 + 2 * 3  ->  +(1, *(2,3))
        prog = parse_source("1 + 2 * 3")
        e = prog.final_expr
        self.assertIsInstance(e, ast.BinOp)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.BinOp)
        self.assertEqual(e.right.op, "*")

    def test_application_binds_tighter(self) -> None:
        from tinyinfer import ast
        prog = parse_source("f x + g y")
        e = prog.final_expr
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.left, ast.App)
        self.assertIsInstance(e.right, ast.App)

    def test_fun_requires_param(self) -> None:
        with self.assertRaises(ParseError):
            parse_source("fun -> 1")

    def test_if_requires_else(self) -> None:
        with self.assertRaises(ParseError):
            parse_source("if true then 1")

    def test_annotation_parsing(self) -> None:
        from tinyinfer import ast
        # 顶层结果标注 + 参数标注 => 组合成完整函数标注 int -> int
        prog = parse_source("let f (x: int): int = x\nf 1")
        b = prog.bindings[0]
        self.assertIsInstance(b.ann, ast.AnnFun)
        self.assertIsInstance(b.ann.param, ast.AnnCon)
        self.assertEqual(b.ann.param.name, "int")
        self.assertEqual(b.ann.result.name, "int")
        # 只有参数标注
        prog2 = parse_source("let g (x: int) = x\ng 1")
        self.assertIsNone(prog2.bindings[0].ann)
        self.assertEqual(prog2.bindings[0].bound.ann.name, "int")

    def test_ref_annotation(self) -> None:
        prog = parse_source("let r: int ref = ref 0\nr")
        self.assertEqual(prog.bindings[0].ann.name, "ref")

    def test_parse_error_has_span(self) -> None:
        try:
            parse_source("let = 1")
        except ParseError as err:
            self.assertIsNotNone(err.span)
            self.assertEqual(err.span.start.line, 1)
        else:  # pragma: no cover
            self.fail("应当报语法错误")

    def test_toplevel_rhs_with_nested_fun_and_let(self) -> None:
        # 无 in 顶层定义的 fun 体内可以含行内 let...in
        prog = parse_source(
            "let f = fun n ->\n"
            "  let acc = ref 0 in\n"
            "  acc <- n; deref acc"
        )
        self.assertEqual(len(prog.bindings), 1)
        # 显式 in 接收尾表达式
        prog2 = parse_source(
            "let f = fun n ->\n"
            "  let acc = ref 0 in\n"
            "  acc <- n; deref acc\n"
            "in f 5"
        )
        self.assertEqual(prog2.bindings, [])
        self.assertIsNotNone(prog2.final_expr)
        # 简单函数体（无嵌套 let）的顶层无 in 定义是允许的
        prog3 = parse_source("let g = fun x -> x + 1\nlet h = g 2")
        self.assertEqual(len(prog3.bindings), 2)

    def test_toplevel_rhs_with_if_then_final_expr(self) -> None:
        # 顶层 let 定义（无 in）后，要用显式 in 才能接收尾表达式
        prog = parse_source("let g x = if x then 1 else 2 in g true")
        self.assertEqual(prog.bindings, [])
        self.assertIsNotNone(prog.final_expr)
        # 纯顶层定义（无 in）则以 unit 收尾
        prog2 = parse_source("let g x = if x then 1 else 2")
        self.assertEqual(len(prog2.bindings), 1)

    def test_not_is_identifier_not_keyword(self) -> None:
        # not 可作为值传递给高阶函数
        prog = parse_source("let twice f x = f (f x) in twice not true")
        self.assertIsNotNone(prog.final_expr)

    def test_multiple_top_level_then_final(self) -> None:
        # 多个顶层定义：最后一个定义的右值可以延伸到 token 流末尾
        prog = parse_source("let a = 1\nlet b = 2\nlet c = 3")
        self.assertEqual(len(prog.bindings), 3)
        # 想在定义后接收尾表达式，必须用 in 显式连接
        prog2 = parse_source("let a = 1 in let b = 2 in a + b")
        self.assertIsNotNone(prog2.final_expr)
        # 顶层定义后裸接表达式会被并入最后一个定义右值；要得到
        # a+b+c 请使用 in
        prog3 = parse_source("let a = 1 in let b = 2 in let c = 3 in a+b+c")
        self.assertEqual(prog3.bindings, [])
        self.assertIsNotNone(prog3.final_expr)

    def test_inline_let_as_only_program(self) -> None:
        prog = parse_source("let x = 1 in x + 1")
        self.assertEqual(prog.bindings, [])
        self.assertIsNotNone(prog.final_expr)


if __name__ == "__main__":
    unittest.main()
