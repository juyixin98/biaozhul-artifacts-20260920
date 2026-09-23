"""解释器测试 + 关键验收性质：验证通过的程序不发生栈下溢。"""

import unittest

from slang.compiler import compile_source
from slang.errors import InvariantBroken, RuntimeErr
from slang.interpreter import Interpreter, ival, format_value


def run(text, **kw):
    m = compile_source(text)
    return Interpreter(m, **kw).run_main()


class TestInterpreter(unittest.TestCase):
    def test_sum_loop(self):
        r = run("""
        fn sumto(int n): int {
            var i = 1; var acc = 0;
            while (i <= n) { acc = acc + i; i = i + 1; }
            return acc;
        }
        fn main() { print sumto(10); }
        """)
        self.assertEqual(r.printed, ["55"])

    def test_early_return(self):
        r = run("""
        fn cls(int x): int {
            if (x < 0) { return -1; }
            if (x == 0) { return 0; }
            return 1;
        }
        fn main() { print cls(-7); print cls(0); print cls(42); }
        """)
        self.assertEqual(r.printed, ["-1", "0", "1"])

    def test_recursion_factorial(self):
        r = run("""
        fn fact(int n): int { if (n <= 1) { return 1; } return n * fact(n - 1); }
        fn main() { print fact(6); }
        """)
        self.assertEqual(r.printed, ["720"])

    def test_bool_output(self):
        r = run("""
        fn f(): bool { return true && !false || false; }
        fn main() { print f(); }
        """)
        self.assertEqual(r.printed, ["true"])

    def test_truncating_div_and_mod(self):
        r = run("""
        fn main() {
            print -7 / 2;
            print -7 % 2;
        }
        """)
        # 向零截断：-7/2 = -3，-7%2 = -1
        self.assertEqual(r.printed, ["-3", "-1"])

    def test_div_by_zero_is_runtime_error(self):
        with self.assertRaises(RuntimeErr):
            run("fn main() { print 1 / 0; }")

    def test_fuel_bounds_infinite_loop(self):
        with self.assertRaises(RuntimeErr):
            run("fn main() { while (true) { } }", fuel=1000)

    def test_depth_bound(self):
        with self.assertRaises(RuntimeErr):
            run("""
            fn loop(int n): int { return loop(n + 1); }
            fn main() { print loop(0); }
            """, max_depth=50)

    def test_call_with_args(self):
        m = compile_source("""
        fn add(int a, int b): int { return a + b; }
        fn main() { print add(2, 3); }
        """)
        it = Interpreter(m)
        r = it.run_main()
        self.assertEqual(r.printed, ["5"])

    def test_format_value(self):
        self.assertEqual(format_value(ival(3)), "3")
        self.assertEqual(format_value(("b", True)), "true")
        self.assertEqual(format_value(None), "void")


class TestRefusesUnverified(unittest.TestCase):
    def test_interpreter_refuses_bad_module(self):
        from slang.bytecode import FuncCode, OP_ADD, OP_RET
        from slang.bytecode import Module
        bad = Module("bad", "", [
            FuncCode("main", [], 0, [], bytearray([OP_ADD, OP_RET]))])
        with self.assertRaises(InvariantBroken):
            Interpreter(bad)   # verify_first=True 默认


if __name__ == "__main__":
    unittest.main()
