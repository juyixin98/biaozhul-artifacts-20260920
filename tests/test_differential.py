"""Differential tests: source interpreter vs closure-converted IR interpreter.

The acceptance criteria are covered here:
  * shadowing (blocks, parameters)
  * recursion (direct, named function expression, mutual)
  * escaping closures (created activation is gone, cell survives)
  * multiple closures sharing one mutable captured binding

Every program must produce IDENTICAL trace lists in both interpreters.
Programs annotated with a leading ``// error:`` line must raise the same
runtime behavior (both error or both succeed).
"""

from __future__ import annotations

import os
import unittest

from slang.errors import LangError
from slang.ir_interp import IRInterpreter
from slang.parser import parse
from slang.source_interp import SourceInterpreter
from slang.analyzer import analyze
from slang.lower import lower_module

EX_DIR = os.path.join(os.path.dirname(__file__), "..", "examples")


def run_both(source: str):
    tree = parse(source)
    src_trace = SourceInterpreter(tree).run()
    analysis = analyze(tree)
    module = lower_module(tree, analysis)
    ir_trace = IRInterpreter(module).run()
    return src_trace, ir_trace


PROGRAMS = {
    "shadow_block": """
let x = 1;
{ let x = 2; print(x); }
print(x);
{ x = 9; print(x); }
print(x);
""",
    "shadow_param": """
let x = 5;
let f = fn(x) { x = x * 2; return x; };
print(f(21));
print(x);
""",
    "shadow_nested_chains": """
let a = 0;
{
  let a = 1;
  {
    let a = 2;
    print(a);
    a = 22;
    print(a);
  }
  print(a);
}
print(a);
""",
    "direct_recursion": """
fn sum(n) {
  if (n == 0) { return 0; }
  return n + sum(n - 1);
}
print(sum(100));
""",
    "named_fn_expr_recursion": """
let fact = fn self(n) {
  if (n <= 1) { return 1; }
  return n * self(n - 1);
};
print(fact(6));
""",
    "mutual_recursion": """
fn even(n) { if (n == 0) { return true; } return odd(n - 1); }
fn odd(n)  { if (n == 0) { return false; } return even(n - 1); }
print(even(20));
print(odd(20));
""",
    "escape_counter": """
fn make(start) {
  let c = fn() { start = start + 1; return start; };
  return c;
}
let a = make(10);
let b = make(50);
print(a());
print(a());
print(b());
print(a());
""",
    "shared_two_closures": """
fn pair() {
  let n = 0;
  let inc = fn() { n = n + 1; return n; };
  let dec = fn() { n = n - 1; return n; };
  return fn(op) {
    if (op == 0) { return inc(); }
    return dec();
  };
}
let p = pair();
print(p(0));
print(p(0));
print(p(0));
print(p(1));
print(p(1));
print(p(0));
""",
    "three_closures_share_param": """
fn account(balance) {
  let deposit = fn(x) { balance = balance + x; return balance; };
  let withdraw = fn(x) { balance = balance - x; return balance; };
  let check = fn() { return balance; };
  return fn(op, x) {
    if (op == 0) { return deposit(x); }
    if (op == 1) { return withdraw(x); }
    return check();
  };
}
let acc = account(100);
print(acc(0, 30));
print(acc(1, 80));
print(acc(2, 0));
print(acc(1, 60));
""",
    "deep_capture": """
fn a(x) {
  return fn() {
    let y = 100;
    return fn() { x = x + 1; y = y + 1; return x * 1000 + y; };
  };
}
let f = a(0)();
print(f());
print(f());
print(f());
""",
    "closure_loop_independent": """
let arr = 0;
let i = 0;
let fns = 0;
while (i < 3) {
  let k = i;
  let g = fn() { return k; };
  print(g());
  k = k + 10;
  print(g());
  i = i + 1;
}
""",
    "mutation_not_visible_when_not_captured": """
let f;
{
  let local = 7;
  f = fn() { return 42; };
  local = 8;
}
print(f());
""",
    "higher_order": """
let apply = fn(f, x) { return f(x); };
let double = fn(n) { return n + n; };
print(apply(double, 21));
""",
    "short_circuit": """
let log = 0;
let t = fn() { return 3; };
print(false && t() == 0);
print(true || t() == 0);
print(0 || 7);
print(1 && 8);
""",
}


class DifferentialTests(unittest.TestCase):
    def test_programs_agree(self):
        for name, src in PROGRAMS.items():
            with self.subTest(name=name):
                s, i = run_both(src)
                self.assertEqual(s, i, f"trace mismatch for {name}: {s} != {i}")

    def test_example_files_agree(self):
        for fn in sorted(os.listdir(EX_DIR)):
            if not fn.endswith(".slang"):
                continue
            with self.subTest(file=fn):
                with open(os.path.join(EX_DIR, fn), encoding="utf-8") as f:
                    source = f.read()
                s, i = run_both(source)
                self.assertEqual(s, i)

    # ---- expected specific values (guards against both being wrong alike) ----

    def test_expected_shared_update_values(self):
        s, i = run_both(PROGRAMS["shared_two_closures"])
        self.assertEqual(s, ["1", "2", "3", "2", "1", "2"])

    def test_expected_escape_independence(self):
        s, i = run_both(PROGRAMS["escape_counter"])
        self.assertEqual(s, ["11", "12", "51", "13"])
        self.assertEqual(i, s)

    def test_expected_deep_capture(self):
        s, i = run_both(PROGRAMS["deep_capture"])
        self.assertEqual(s, ["1101", "2102", "3103"])


class RuntimeErrorAgreementTests(unittest.TestCase):
    def _both_error(self, src):
        tree = parse(src)
        src_errored = False
        try:
            SourceInterpreter(tree).run()
        except LangError:
            src_errored = True
        ir_errored = False
        try:
            analysis = analyze(tree)
            module = lower_module(tree, analysis)
            IRInterpreter(module).run()
        except LangError:
            ir_errored = True
        return src_errored, ir_errored

    def test_unbound_name_is_compile_error(self):
        from slang.errors import CompileError
        from slang.analyzer import analyze
        from slang.parser import parse

        with self.assertRaises(CompileError):
            analyze(parse("print(missing);\n"))

    def test_div_by_zero_both(self):
        s, i = self._both_error("print(1 / 0);\n")
        self.assertTrue(s and i)

    def test_arity_both(self):
        s, i = self._both_error("let f = fn(a) { return a; }; print(f());\n")
        self.assertTrue(s and i)

    def test_call_non_function_both(self):
        s, i = self._both_error("let x = 3; print(x());\n")
        self.assertTrue(s and i)

    def test_uninitialized_let_is_null(self):
        src, ir = run_both("let x; print(x);\n")
        self.assertEqual(src, ["null"])
        self.assertEqual(ir, src)

    def test_fn_only_available_after_declaration(self):
        # Names are hoisted but closures initialize at the declaration stmt.
        s, i = self._both_error("print(early(4)); fn early(n) { return n + 1; }")
        self.assertTrue(s and i)

    def test_declared_fn_is_callable_afterward(self):
        src, ir = run_both("fn f(n){return n+1;} print(f(4));")
        self.assertEqual(src, ["5"])
        self.assertEqual(ir, src)

    def test_mutual_recursion_works(self):
        src, ir = run_both(
            "fn e(n){if(n==0){return 1;} return o(n-1);} "
            "fn o(n){if(n==0){return 0;} return e(n-1);} print(e(6));"
        )
        self.assertEqual(src, ["1"])
        self.assertEqual(ir, src)


if __name__ == "__main__":
    unittest.main()
