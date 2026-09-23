"""Tests for the hand-written lexer, parser and IR lowering (locations)."""

import unittest

from interval_ai.errors import LexError, ParseError, AnalysisError
from interval_ai.lexer import Lexer
from interval_ai.parser import parse_source
from interval_ai.ir import build_cfg
from interval_ai import ir


class TestLexer(unittest.TestCase):
    def test_spans_line_col(self):
        src = "var x = 12;\ny = x + 2;"
        toks = Lexer(src).tokenize()
        kinds = [t.kind for t in toks]
        self.assertIn("INT", kinds)
        int_tok = next(t for t in toks if t.kind == "INT")
        self.assertEqual(int_tok.value, 12)
        self.assertEqual((int_tok.span.line, int_tok.span.col), (1, 9))
        # Token on line 2.
        y_tok = next(t for t in toks if t.kind == "IDENT" and t.text == "y")
        self.assertEqual(y_tok.span.line, 2)
        self.assertEqual(y_tok.span.col, 1)

    def test_keywords_and_operators(self):
        src = "if (a <= b && c != 0) {} else {}"
        kinds = [t.kind for t in Lexer(src).tokenize()]
        self.assertIn("<=", kinds)
        self.assertIn("&&", kinds)
        self.assertIn("!=", kinds)

    def test_comments(self):
        src = """
        // line comment
        /* block /* nested */ still */ var z;
        """
        toks = Lexer(src).tokenize()
        self.assertEqual([t.kind for t in toks if t.kind not in ("EOF",)],
                         ["var", "IDENT", ";"])

    def test_unterminated_block_comment(self):
        with self.assertRaises(LexError):
            Lexer("/* never closed").tokenize()

    def test_bad_char(self):
        with self.assertRaises(LexError):
            Lexer("var @;").tokenize()

    def test_big_integer(self):
        src = "var x = 123456789012345678901234567890;"
        prog = parse_source(src)
        self.assertEqual(prog.decls[0].init.value,
                         123456789012345678901234567890)


class TestParser(unittest.TestCase):
    def test_minimal_program(self):
        p = parse_source("var x; skip;")
        # One declaration, one body statement.
        self.assertEqual(len(p.decls), 1)
        self.assertEqual(len(p.body.stmts), 1)

    def test_braced_or_bare_body(self):
        self.assertIsNotNone(parse_source("var x; { skip; }"))
        self.assertIsNotNone(parse_source("var x; skip;"))

    def test_precedence(self):
        # 1 + 2 * 3
        p = parse_source("var x = 1 + 2 * 3;")
        from interval_ai import ast_nodes as ast
        e = p.decls[0].init
        self.assertIsInstance(e, ast.Binary)
        self.assertEqual(e.op, "+")
        self.assertIsInstance(e.right, ast.Binary)
        self.assertEqual(e.right.op, "*")

    def test_comparison_non_assoc_chain_rejected(self):
        with self.assertRaises(ParseError):
            parse_source("var x = a < b < c;")

    def test_missing_semicolon(self):
        with self.assertRaises(ParseError):
            parse_source("var x = 1")

    def test_unclosed_paren(self):
        with self.assertRaises(ParseError):
            parse_source("if (true { skip; }")

    def test_input_array_decl(self):
        p = parse_source("input array buf[8]; skip;")
        self.assertEqual(p.decls[0].kind, "array")
        self.assertEqual(p.decls[0].length, 8)


class TestIR(unittest.TestCase):
    def build(self, src):
        return build_cfg(parse_source(src), src)

    def test_undeclared_var(self):
        with self.assertRaises(AnalysisError):
            self.build("x = 1;")

    def test_array_used_as_scalar(self):
        with self.assertRaises(AnalysisError):
            self.build("input array a[3]; a = 1;")

    def test_scalar_used_as_array(self):
        with self.assertRaises(AnalysisError):
            self.build("var x; x[0] = 1;")

    def test_division_operator_span(self):
        src = "var a = 7;\nvar b = 2;\nvar c = a / b;"
        cfg = self.build(src)
        spans = []
        for blk in cfg.blocks:
            for ins in blk.instrs:
                if isinstance(ins, ir.IAssign):
                    self.collect_bin(ins.value, spans)
        op = next(s for s in spans)
        self.assertEqual(src[op.start_offset:op.end_offset], "/")
        self.assertEqual(op.line, 3)

    def collect_bin(self, e, out):
        if isinstance(e, ir.RBin):
            out.append(e.op_span)
            self.collect_bin(e.left, out)
            self.collect_bin(e.right, out)
        elif isinstance(e, ir.RNeg):
            self.collect_bin(e.operand, out)

    def test_loop_shape(self):
        src = "var i=0; while (i < 3) { i = i + 1; }"
        cfg = self.build(src)
        branches = [b for b in cfg.blocks
                    if isinstance(b.terminator, ir.Branch)]
        self.assertEqual(len(branches), 1)
        br = branches[0].terminator
        # Body jumps back to the header.
        body_block = cfg.blocks[br.then]
        self.assertIsInstance(body_block.terminator, ir.Jump)
        self.assertEqual(body_block.terminator.target, branches[0].id)

    def test_local_decl_stays_in_branch(self):
        src = ("input var k;\n"
               "if (k > 0) {\n"
               "  var z = 10 / k;\n"
               "}\n")
        cfg = self.build(src)
        # The division must NOT be evaluated in the entry block; z may be
        # zero-initialized there, but no RBin "/" lives in block 0.
        entry = cfg.blocks[cfg.entry]

        def has_div(e):
            if isinstance(e, ir.RBin):
                if e.op == "/":
                    return True
                return has_div(e.left) or has_div(e.right)
            if isinstance(e, ir.RNeg):
                return has_div(e.operand)
            if isinstance(e, ir.RArrayLoad):
                return has_div(e.index)
            return False

        for ins in entry.instrs:
            if isinstance(ins, ir.IAssign):
                self.assertFalse(has_div(ins.value))
        divisions = 0
        for blk in cfg.blocks:
            for ins in blk.instrs:
                if isinstance(ins, ir.IAssign) and isinstance(
                        ins.value, ir.RBin) and ins.value.op == "/":
                    divisions += 1
        self.assertEqual(divisions, 1)


if __name__ == "__main__":
    unittest.main()
