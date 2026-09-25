"""安全表达式解析器测试：白名单放行、注入拒绝、数学错误映射。"""

import math
import unittest

from adaptive_integration import integrate
from adaptive_integration.parser import ParseError, ParsedFunction


class TestParsingAccepts(unittest.TestCase):
    def test_basic_expressions(self):
        cases = {
            "x": 0.5,
            "2*x + 1": 2.0,
            "exp(-x**2)": math.exp(-0.25),
            "sin(pi*x) + cos(2*pi*x)": math.sin(0.5 * math.pi) + math.cos(math.pi),
            "log(1+x)/sqrt(1+x**2)": math.log(1.5) / math.sqrt(1.25),
            "atan2(x, 1) + pow(2, -x)": math.atan2(0.5, 1) + 2 ** -0.5,
            "erf(x) + gamma(1+x)": math.erf(0.5) + math.gamma(1.5),
            "fmod(x, 0.3)": math.fmod(0.5, 0.3),
        }
        for expr, expected in cases.items():
            with self.subTest(expr=expr):
                self.assertAlmostEqual(ParsedFunction(expr)(0.5),
                                       expected, places=12)

    def test_aliases(self):
        self.assertAlmostEqual(ParsedFunction("ln(e)")(0.0), 1.0, places=12)
        self.assertAlmostEqual(ParsedFunction("arcsin(1)")(0.0),
                               math.pi / 2, places=12)


class TestParsingRejects(unittest.TestCase):
    INJECTIONS = [
        "__import__('os').system('echo pwned')",
        "x.__class__",
        "(lambda: 1)()",
        "[i for i in range(10)]",
        "x if x > 0 else -x",          # IfExp 不允许
        "x > 0",                        # 比较不允许
        "x and 1",                      # 布尔不允许
        "open('README.md').read()",
        "sin(x, extra=1)",
        "cos",                          # 函数名裸用
        "y + 1",                        # 未知变量
        "1 + 2j",                       # 复数
        "'hello'",                      # 字符串
        "True",                         # 布尔
        "float('inf')",                 # 函数不在白名单 + 字符串
        "9**9**9**9**9",                # 字面量运算爆炸（编译期不炸，求值炸）
    ]

    def test_injections_rejected_or_nonfinite(self):
        for expr in self.INJECTIONS:
            with self.subTest(expr=expr):
                try:
                    f = ParsedFunction(expr)
                except ParseError:
                    continue
                # 唯一可能通过语法检查的是指数爆炸：求值必须给出 inf 而非挂起
                y = f(0.5)
                self.assertFalse(math.isfinite(y))

    def test_caret_is_not_power(self):
        with self.assertRaises(ParseError):
            ParsedFunction("x^2")

    def test_wrong_arity(self):
        with self.assertRaises(ParseError):
            ParsedFunction("pow(x)")
        with self.assertRaises(ParseError):
            ParsedFunction("sin(x, x)")

    def test_size_limits(self):
        with self.assertRaises(ParseError):
            ParsedFunction("x" * 201)
        with self.assertRaises(ParseError):
            ParsedFunction("x" + "+x" * 40)  # 深 AST（括号本身不产生节点）
        with self.assertRaises(ParseError):
            ParsedFunction("1e9")

    def test_empty(self):
        with self.assertRaises(ParseError):
            ParsedFunction("   ")


class TestEvalMapping(unittest.TestCase):
    def test_domain_errors_become_nonfinite(self):
        # log(0) 在区间内部 x=0.5 处；端点有限，因此归类为内部奇点
        r = integrate(lambda x: ParsedFunction("log(fabs(x-0.5))")(x),
                      0.0, 1.0, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "SINGULAR_INTERIOR")

    def test_division_by_zero_at_endpoint(self):
        r = integrate(lambda x: ParsedFunction("1/x")(x),
                      0.0, 1.0, 1e-8, 1e-8)
        self.assertFalse(r.converged)
        self.assertEqual(r.error_code, "SINGULAR_ENDPOINT")


if __name__ == "__main__":
    unittest.main(verbosity=2)
