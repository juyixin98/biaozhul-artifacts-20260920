import os
import unittest

from tests._util import compile3, equivalent, read_example, signature


class TestInterpreterEquivalence(unittest.TestCase):
    def assertEquivalent(self, src):
        a, i, o = compile3(src)
        sa, si, so = signature(a), signature(i), signature(o)
        self.assertEqual(sa, si, f"AST vs IR differ:\n{src}")
        self.assertEqual(sa, so, f"AST vs optimized differ:\n{src}")

    def test_empty_program(self):
        self.assertEquivalent("")

    def test_arithmetic(self):
        self.assertEquivalent("print 2 + 3 * 4 - 1;")
        self.assertEquivalent("print (2 + 3) * 4;")

    def test_uninitialized_variable_is_zero(self):
        self.assertEquivalent("print z;")
        self.assertEquivalent("print z + 5;")

    def test_comparisons(self):
        for op in ("==", "!=", "<", "<=", ">", ">="):
            self.assertEquivalent(f"print 3 {op} 3;")
            self.assertEquivalent(f"print 2 {op} 5;")

    def test_logical_non_short_circuit_semantics(self):
        # && / || return 0/1; operands both evaluated (no short circuit)
        self.assertEquivalent("print 1 && 0;")
        self.assertEquivalent("print 1 || 0;")
        self.assertEquivalent("print 0 || 0;")
        # side effects would differ if short-circuiting existed; here the
        # only observable side effect would be a trap:
        self.assertEquivalent("print 0 && (1/0 == 0);")  # both sides run

    def test_branch_unreachable(self):
        self.assertEquivalent("if 1 { print 1; } else { print 2; }")
        self.assertEquivalent("if 0 { print 1; } else { print 2; }")

    def test_branch_dynamic(self):
        self.assertEquivalent(
            "i := 0; while i < 1 { i := i + 1; }"
            "if i { print 10; } else { print 20; }")

    def test_zero_trip_loop(self):
        self.assertEquivalent(read_example("zero_trip_loop.lat"))

    def test_counting_loop(self):
        self.assertEquivalent(read_example("loop_materializes.lat"))

    def test_confluence(self):
        self.assertEquivalent(read_example("confluence.lat"))

    def test_divzero_reachable_same_error(self):
        a, i, o = compile3(read_example("divzero_reachable.lat"))
        for r in (a, i, o):
            self.assertFalse(r.ok)
            self.assertEqual(r.error.message, "division or modulo by zero")
        # span identical across all three
        self.assertEqual(a.error.span.offset, i.error.span.offset)
        self.assertEqual(a.error.span.offset, o.error.span.offset)

    def test_divzero_unreachable_no_error(self):
        self.assertEquivalent(read_example("divzero_unreachable.lat"))

    def test_dynamic_divzero_same_error(self):
        a, i, o = compile3(read_example("divzero_dynamic.lat"))
        for r in (a, i, o):
            self.assertFalse(r.ok)
            self.assertEqual(r.error.message, "division or modulo by zero")

    def test_modulo_semantics(self):
        self.assertEquivalent(read_example("mod_negative.lat"))

    def test_side_effect_ordering(self):
        self.assertEquivalent(read_example("side_effects.lat"))

    def test_nested_control_flow(self):
        src = ("i := 0;\n"
               "while i < 4 {\n"
               "  if i < 2 { print i + 10; } else { print i + 20; }\n"
               "  i := i + 1;\n"
               "}\nprint 99;")
        self.assertEquivalent(src)

    def test_all_shipped_examples(self):
        examples_dir = os.path.join(os.path.dirname(__file__), "..", "examples")
        for name in sorted(os.listdir(examples_dir)):
            if name.endswith(".lat"):
                with open(os.path.join(examples_dir, name), encoding="utf-8") as f:
                    src = f.read()
                self.assertTrue(equivalent(src), f"equivalence failed: {name}")


if __name__ == "__main__":
    unittest.main()
