"""Unit tests for scope analysis, escape analysis and closure conversion."""

import unittest

from slang import ir_nodes as ir
from slang.analyzer import analyze
from slang.errors import CompileError
from slang.lower import lower_module
from slang.parser import parse


def compile_src(src):
    tree = parse(src)
    analysis = analyze(tree)
    module = lower_module(tree, analysis)
    return tree, analysis, module


def funcs_by_name(module):
    return {f.name: f for f in module.funcs} | {module.main.name: module.main}


class AnalysisTests(unittest.TestCase):
    def test_shadowing_gets_distinct_slots(self):
        _, a, _ = compile_src("let x = 1; { let x = 2; print(x); } print(x);")
        main = a.frames["main"]
        self.assertEqual([v.name for v in main.slots], ["x", "x"])
        self.assertNotEqual(main.slots[0].slot, main.slots[1].slot)

    def test_captured_variable_is_boxed(self):
        _, a, _ = compile_src("fn f() { let x = 0; let g = fn() { x = 1; return x; }; return g; }")
        f = next(v for v in a.frames.values() if v.name == "f")
        x = next(v for v in f.slots if v.name == "x")
        self.assertTrue(x.boxed)
        self.assertIn(x.slot, f.boxed_slots)
        g = next(v for v in a.frames.values() if v.name == "<lambda>")
        self.assertEqual([fr.var.name for fr in g.free], ["x"])

    def test_uncaptured_local_is_not_boxed(self):
        _, a, _ = compile_src("fn f() { let x = 0; x = 1; return x; }")
        f = next(v for v in a.frames.values() if v.name == "f")
        self.assertEqual(f.boxed_slots, [])

    def test_captured_parameter_is_boxed(self):
        _, a, _ = compile_src("fn f(p) { return fn() { p = p + 1; return p; }; }")
        f = next(v for v in a.frames.values() if v.name == "f")
        self.assertIn(0, f.boxed_slots)

    def test_unbound_name(self):
        with self.assertRaises(CompileError):
            compile_src("print(no_such_name);")

    def test_duplicate_let_in_block(self):
        with self.assertRaises(CompileError):
            compile_src("{ let a = 1; let a = 2; }")

    def test_duplicate_function_name(self):
        with self.assertRaises(CompileError):
            compile_src("fn a() {} fn a() {}")

    def test_duplicate_parameter(self):
        with self.assertRaises(CompileError):
            compile_src("fn f(a, a) {}")

    def test_recursion_captures_self_name(self):
        # Direct recursion references the hoisted binding in the parent frame;
        # the escape analyzer boxes it and lists it as a free variable.
        _, a, _ = compile_src("fn rec(n) { return rec(n); }")
        rec = next(v for v in a.frames.values() if v.name == "rec")
        self.assertEqual([fr.var.name for fr in rec.free], ["rec"])
        self.assertTrue(rec.free[0].var.boxed)
        main = a.frames["main"]
        rec_var = next(v for v in main.slots if v.name == "rec")
        self.assertTrue(rec_var.boxed)

    def test_transitive_capture_threads_intermediate(self):
        src = """
fn outer(v) {
  let mid = fn() {
    return fn() { return v; };
  };
  return mid;
}
"""
        _, a, _ = compile_src(src)
        mid = next(f for f in a.frames.values() if f.name == "<lambda>" and f.parent and f.parent.name == "outer")
        # mid must capture v so it can pass the cell to its own child.
        self.assertTrue(any(c.var.name == "v" for c in mid.captures))

    def test_print_builtin(self):
        _, a, _ = compile_src("print(1);")
        main = a.frames["main"]
        # No local slot is allocated for print.
        self.assertFalse(any(v.name == "print" for v in main.slots))

    def test_shadow_print_with_let(self):
        _, a, mod = compile_src("{ let print = 7; } print(1);")
        main = a.frames["main"]
        # A slot exists for the shadowing let, but outer print remains builtin.
        self.assertTrue(any(v.name == "print" for v in main.slots))


class LoweringTests(unittest.TestCase):
    def test_free_closure_receives_cell(self):
        _, _, mod = compile_src("fn f() { let x = 0; return fn() { return x; }; }")
        f = next(f for f in mod.funcs if f.name == "f")
        # The captured let cell is created at its declaration statement
        # (NEW_CELL), then passed through MAKE_CLOSURE.
        ops = []
        for st in f.body.stmts:
            collect_ops(st, ops)
        self.assertIn("NEW_CELL", ops)
        self.assertIn("MAKE_CLOSURE", ops)
        # The inner function declares one capture named x.
        inner = next(f for f in mod.funcs if f.name == "<lambda>")
        self.assertEqual([c["name"] for c in inner.captures], ["x"])

    def test_box_read_and_write(self):
        _, _, mod = compile_src("fn f() { let x = 0; let g = fn() { x = x + 1; }; g(); return x; }")
        inner = next(f for f in mod.funcs if f.name == "<lambda>")
        ops = []
        for st in inner.body.stmts:
            collect_ops(st, ops)
        self.assertIn("CELL_DEREF", ops)
        self.assertIn("CELL_SET", ops)
        self.assertIn("GET_FREE", ops)
        self.assertNotIn("SET_LOCAL", ops)

    def test_named_fn_expr_self_slot(self):
        _, _, mod = compile_src("let f = fn self(n) { return self(n); };")
        self_fn = next(f for f in mod.funcs if f.self_slot is not None)
        self.assertTrue(any(c.get("self") for c in self_fn.captures))

    def test_functions_lifted(self):
        _, _, mod = compile_src("let f = fn() { let g = fn() { return 1; }; return g(); };")
        names = {f.name for f in mod.funcs}
        self.assertIn("<lambda>", names)
        self.assertEqual(len(mod.funcs), 2)

    def test_module_is_json_serializable(self):
        import json
        _, _, mod = compile_src("print(1 + 2);")
        json.dumps(mod.to_dict())  # must not raise

    def test_constants_dedup(self):
        _, _, mod = compile_src("print(1); print(1); print(2);")
        self.assertEqual(sorted(mod.constants), [1, 2])


def collect_ops(stmt, out):
    if isinstance(stmt, ir.IExprStmt):
        out.extend(i.op for i in stmt.instrs)
    elif isinstance(stmt, ir.IReturn):
        if stmt.instrs:
            out.extend(i.op for i in stmt.instrs)
    elif isinstance(stmt, ir.IIf):
        out.extend(i.op for i in stmt.cond)
        for s in stmt.then.stmts:
            collect_ops(s, out)
    elif isinstance(stmt, ir.IWhile):
        out.extend(i.op for i in stmt.cond)
        for s in stmt.body.stmts:
            collect_ops(s, out)
    elif isinstance(stmt, ir.IBlock):
        for s in stmt.stmts:
            collect_ops(s, out)


if __name__ == "__main__":
    unittest.main()
