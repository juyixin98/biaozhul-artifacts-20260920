"""运算语义与 AST/IR 解释器测试。"""

import unittest

from constprop.interp import run_ast
from constprop.ir_interp import run_ir
from constprop.cfg import build_cfg
from constprop.parser import parse_source
from constprop.runtime import apply_binop, trunc_div, trunc_mod
from constprop.source import (
    DIV_ZERO,
    STACK_LIMIT,
    UNDEFINED_VAR,
    SourceText,
)
from constprop.ssa import construct_ssa


def ast_run(text, limit=2_000_000):
    src = SourceText(text)
    return run_ast(parse_source(src), src, step_limit=limit)


def ir_run(text, limit=2_000_000):
    src = SourceText(text)
    cfg = build_cfg(parse_source(src), src)
    construct_ssa(cfg)
    return run_ir(cfg, src, step_limit=limit)


class TestArithmetic(unittest.TestCase):
    def test_truncation_toward_zero(self):
        self.assertEqual(trunc_div(7, 3), 2)
        self.assertEqual(trunc_div(-7, 3), -2)
        self.assertEqual(trunc_div(7, -3), -2)
        self.assertEqual(trunc_mod(-7, 3), -1)
        self.assertEqual(trunc_mod(7, -3), 1)

    def test_comparisons_yield_01(self):
        self.assertEqual(apply_binop("<", 1, 2), 1)
        self.assertEqual(apply_binop("==", 2, 2), 1)
        self.assertEqual(apply_binop("!=", 2, 2), 0)

    def test_divide_by_zero_raises(self):
        for op in ("/", "%"):
            with self.assertRaises(Exception) as ctx:
                apply_binop(op, 5, 0)
            self.assertEqual(ctx.exception.code, DIV_ZERO)


class TestInterpreter(unittest.TestCase):
    def test_simple_output(self):
        r = ast_run("print 1 + 2 * 3;\n")
        self.assertEqual(r.output, [7])
        self.assertTrue(r.ok)

    def test_ast_and_ir_agree_simple(self):
        text = "x=3;\nwhile(x){print x;x=x-1;}\nprint 10/2;\n"
        a, i = ast_run(text), ir_run(text)
        self.assertEqual(a.signature(), i.signature())
        self.assertEqual(a.output, [3, 2, 1, 5])

    def test_short_circuit(self):
        # a=0 时 && 右操作数不求值：右操作数里放除零，验证不被执行
        t = "a=0;\nprint a && (1/0);\nprint 1;\n"
        r = ast_run(t)
        self.assertEqual(r.output, [0, 1])
        self.assertTrue(r.ok)
        self.assertEqual(r.signature(), ir_run(t).signature())

    def test_short_circuit_or(self):
        # a=1 时 || 右操作数不求值
        t = "a=1;\nprint a || (1/0);\nprint 1;\n"
        r = ast_run(t)
        self.assertEqual(r.output, [1, 1])
        self.assertTrue(r.ok)
        self.assertEqual(r.signature(), ir_run(t).signature())

    def test_short_circuit_right_evaluated_when_needed(self):
        # 左真对 && 要求右值；右值除零则必须报错（没有错误地跳过）
        t = "print 1 && (1/0);\n"
        r = ast_run(t)
        self.assertEqual(r.error_code, DIV_ZERO)
        self.assertEqual(r.signature(), ir_run(t).signature())

    def test_flat_scope_block_does_not_hide(self):
        t = "if (1) { y = 20; } else { y = 40; }\nprint y;\n"
        r = ast_run(t)
        self.assertEqual(r.output, [20])

    def test_undefined_variable(self):
        r = ast_run("print 1;\nprint never;\n")
        self.assertFalse(r.ok)
        self.assertEqual(r.error_code, UNDEFINED_VAR)
        self.assertEqual(r.output, [1])  # 错误前输出保留
        self.assertEqual(r.location[0], 2)

    def test_division_by_zero(self):
        r = ast_run("print 1;\nz = 5 / 0;\nprint 2;\n")
        self.assertEqual(r.error_code, DIV_ZERO)
        self.assertEqual(r.output, [1])
        self.assertEqual(r.location[0], 2)

    def test_ir_div_zero_matches_ast(self):
        text = "print 1;\nb=0;\nprint 3/b;\nprint 4;\n"
        a, i = ast_run(text), ir_run(text)
        self.assertEqual(a.signature(), i.signature())
        self.assertEqual(a.error_code, DIV_ZERO)
        self.assertEqual(a.output, [1])

    def test_step_limit_infinite_loop(self):
        r = ast_run("while (1) { x = 1; }\n", limit=1000)
        self.assertEqual(r.error_code, STACK_LIMIT)

    def test_negative_numbers_via_unary(self):
        r = ast_run("x = -5;\nprint -x;\n")
        self.assertEqual(r.output, [5])

    def test_unary_minus_vs_binary_subtract_disambiguation(self):
        # '-' 同时是一元负号与二元减法；多条语句连续时不得互相误分派
        t = ("print -7/3;\nprint -7%3;\nprint 7/-3;\n"
             "print -7/-3;\nprint 10-3;\nprint -10-3;\n")
        a, i = ast_run(t), ir_run(t)
        self.assertEqual(a.signature(), i.signature())
        # 向零截断：-7/3=-2, -7%3=-1, 7/-3=-2, -7/-3=2, 10-3=7, -10-3=-13
        self.assertEqual(a.output, [-2, -1, -2, 2, 7, -13])


class TestUndefSemantics(unittest.TestCase):
    def test_undef_propagates_through_dead_expression(self):
        # 读未定义变量做算术，但结果从不观察 -> 无错误
        t = "b = never + 1;\nx = 2;\nprint x;\n"
        r = ast_run(t)
        self.assertTrue(r.ok)
        self.assertEqual(r.output, [2])
        self.assertEqual(r.signature(), ir_run(t).signature())

    def test_undef_observed_in_print_errors(self):
        t = "b = never + 1;\nprint 2;\nprint b;\n"
        for r in (ast_run(t), ir_run(t)):
            self.assertEqual(r.error_code, UNDEFINED_VAR)
            self.assertEqual(r.output, [2])
            self.assertEqual(r.location[0], 3)

    def test_undef_observed_in_branch_condition_errors(self):
        t = "b = never;\nprint 1;\nif (b) { print 2; }\n"
        for r in (ast_run(t), ir_run(t)):
            self.assertEqual(r.error_code, UNDEFINED_VAR)
            self.assertEqual(r.output, [1])
            self.assertEqual(r.location[0], 3)

    def test_undef_in_division_does_not_mask_as_divzero(self):
        # 除数/被除数为 undef：结果 undef，不触发除零；观察时报 undef
        t = "print 1;\nc = 1 / never;\nprint 2;\nprint c;\n"
        for r in (ast_run(t), ir_run(t)):
            self.assertEqual(r.error_code, UNDEFINED_VAR)
            self.assertEqual(r.output, [1, 2])
            self.assertEqual(r.location[0], 4)

    def test_real_divzero_takes_precedence_when_defined(self):
        t = "print 1;\nz = 5 / (2-2);\nprint 2;\n"
        for r in (ast_run(t), ir_run(t)):
            self.assertEqual(r.error_code, DIV_ZERO)
            self.assertEqual(r.output, [1])
            self.assertEqual(r.location[0], 2)

    def test_variable_rebound_after_undef_works(self):
        t = "x = undef_then_set;\nx = 5;\nprint x;\n"
        r = ast_run(t)
        self.assertEqual(r.output, [5])
        self.assertEqual(r.signature(), ir_run(t).signature())

    def test_undef_feeds_const_zero_branch_then_removed(self):
        # undef 死表达式 + 后续正常常量分支，行为正常
        t = "q = missing * 0;\nif (1) { print 7; } else { print 8; }\n"
        for r in (ast_run(t), ir_run(t)):
            self.assertTrue(r.ok)
            self.assertEqual(r.output, [7])


if __name__ == "__main__":
    unittest.main()
