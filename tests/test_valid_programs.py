"""合法程序：编译/验证/解释器端到端。"""

import io
import unittest

from byteverifier.common import ToolError
from byteverifier.compiler import compile_source
from byteverifier.interpreter import Interpreter
from byteverifier.pipeline import compile_and_verify
from byteverifier.verifier import verify_module


def run_src(src: str, fuel: int = 100_000):
    module = compile_and_verify(src)
    buf = io.StringIO()
    interp = Interpreter(module, fuel=fuel, out=buf)
    result = interp.call_main()
    return result, interp.steps, buf.getvalue()


LOOP_SUM = """
void main() {
    int s = 0;
    int i = 1;
    while (i <= 10) { s = s + i; i = i + 1; }
    print_int(s);
}
"""

RECURSION = """
int fib(int n) {
    if (n < 2) { return n; }
    return fib(n - 1) + fib(n - 2);
}
void main() { print_int(fib(10)); }
"""

BOOL_LOGIC = """
void main() {
    bool a = true;
    bool b = false;
    print_bool(a && b);
    print_bool(a || b);
    print_bool(!(a == b));
    print_bool(3 != 4);
    print_bool(3 >= 3);
}
"""

EARLY_RETURN = """
int classify(int n) {
    if (n < 0) { return -1; }
    if (n == 0) { return 0; }
    return 1;
}
void main() {
    print_int(classify(-7));
    print_int(classify(0));
    print_int(classify(42));
}
"""

VOID_CALL = """
void greet(int n) { print_int(n + 1); }
void main() {
    int i = 0;
    while (i < 3) { greet(i); i = i + 1; }
}
"""

DIV_MOD = """
void main() {
    print_int(20 / 6);
    print_int(7 - 10);
    print_bool(2 < 1);
}
"""


class TestValidPrograms(unittest.TestCase):
    def test_loop_sum(self):
        _, _, out = run_src(LOOP_SUM)
        self.assertEqual(out.strip(), "55")

    def test_recursion(self):
        _, _, out = run_src(RECURSION)
        self.assertEqual(out.strip(), "55")

    def test_bool_logic(self):
        _, _, out = run_src(BOOL_LOGIC)
        self.assertEqual(out.split(), ["false", "true", "true", "true", "true"])

    def test_early_return(self):
        _, _, out = run_src(EARLY_RETURN)
        self.assertEqual(out.split(), ["-1", "0", "1"])

    def test_void_call(self):
        _, _, out = run_src(VOID_CALL)
        self.assertEqual(out.split(), ["1", "2", "3"])

    def test_div_and_cmp(self):
        _, _, out = run_src(DIV_MOD)
        self.assertEqual(out.split(), ["3", "-3", "false"])

    def test_backedge_converge_locals(self):
        # 循环变量在循环头合流：入口已初始化，回边也已初始化，应当通过
        src = """
        void main() {
            int i = 0;
            int acc = 0;
            while (i < 5) { acc = acc + i; i = i + 1; }
            print_int(acc);
        }
        """
        _, _, out = run_src(src)
        self.assertEqual(out.strip(), "10")

    def test_module_roundtrip(self):
        m1 = compile_source(LOOP_SUM)
        blob = m1.encode()
        from byteverifier.bytecode import decode_module
        m2 = decode_module(blob)
        verify_module(m2)
        self.assertEqual([f.name for f in m2.functions],
                         [f.name for f in m1.functions])

    def test_fuel_guard(self):
        src = "void main() { while (true) { } }"
        with self.assertRaises(ToolError) as cm:
            run_src(src, fuel=500)
        self.assertEqual(cm.exception.kind, "fuel.exhausted")

    def test_depth_guard(self):
        src = """
        int inf(int n) { return inf(n + 1); }
        void main() { print_int(inf(0)); }
        """
        with self.assertRaises(ToolError) as cm:
            run_src(src, fuel=1_000_000)
        self.assertEqual(cm.exception.kind, "depth.exceeded")

    def test_div_zero_is_runtime(self):
        src = """
        int half(int n) { return n / 0; }
        void main() { print_int(half(9)); }
        """
        # 静态合法，运行期报错
        with self.assertRaises(ToolError) as cm:
            run_src(src)
        self.assertEqual(cm.exception.kind, "div.zero")


if __name__ == "__main__":
    unittest.main()
