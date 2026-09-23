"""Tests for lexical-scope analysis: shadowing, capture sets, boxing."""
import unittest

from sclang import ast_nodes as ast
from sclang.errors import CompileError
from sclang.parser import parse_source
from sclang.resolver import resolve_program, Binding, FunctionInfo


def analyze(src):
    return resolve_program(parse_source(src))


def funcs_by_name(res):
    return {fi.name: fi for fi in res.functions}


def bind(res, name):
    return [b for b in res.bindings if b.name == name]


class TestShadowing(unittest.TestCase):
    def test_block_shadow_is_distinct_binding(self):
        res = analyze("let x=1; { let x=2; print(x); } print(x);")
        xs = bind(res, "x")
        self.assertEqual(len(xs), 2)
        self.assertIsNot(xs[0], xs[1])

    def test_duplicate_in_same_scope_rejected(self):
        with self.assertRaises(CompileError):
            analyze("let x=1; let x=2;")

    def test_param_then_let_same_scope_allowed(self):
        # parameter scope and body block scope are different scopes
        res = analyze("fn f(x){ let x = 2; }")
        self.assertGreaterEqual(len(bind(res, "x")), 2)

    def test_inner_shadows_outer_free_var(self):
        # inner x binds locally; outer closure binds the outer x
        res = analyze("""
        fn outer(){
          let x = 1;
          fn c1(){ return x; }
          {
            let x = 2;
            fn c2(){ return x; }
          }
        }
        """)
        fb = funcs_by_name(res)
        c1_free = [b for b in fb["c1"].free if b.name == "x"]
        c2_free = [b for b in fb["c2"].free if b.name == "x"]
        self.assertEqual(len(c1_free), 1)
        self.assertEqual(len(c2_free), 1)
        self.assertIsNot(c1_free[0], c2_free[0])  # different x


class TestFreeVariables(unittest.TestCase):
    def test_direct_capture(self):
        res = analyze("fn o(){ let v=1; fn i(){ return v; } }")
        fb = funcs_by_name(res)
        free = [(b.name, b.owner) for b in fb["i"].free]
        self.assertEqual(free, [("v", fb["o"].func_id)])

    def test_transitive_forwarding(self):
        res = analyze("""
        fn o(){ let v=1;
          fn m(){ fn i(){ return v; } }
        }
        """)
        fb = funcs_by_name(res)
        self.assertEqual([b.name for b in fb["m"].free], ["v"])
        self.assertEqual([b.name for b in fb["i"].free], ["v"])

    def test_unbound_rejected(self):
        with self.assertRaises(CompileError):
            analyze("print(nope);")

    def test_assignment_to_builtin_rejected(self):
        # print is a keyword, but ensure plain rebind logic is sound
        with self.assertRaises(CompileError):
            analyze("print = 3;")


class TestBoxing(unittest.TestCase):
    def test_mutated_escape_is_boxed(self):
        res = analyze("fn o(){ let c=0; fn i(){ c=c+1; } }")
        c = [b for b in res.bindings if b.name == "c"][0]
        self.assertTrue(c.captured and c.mutated and c.boxed)

    def test_captured_readonly_is_boxed_but_unmutated(self):
        res = analyze("fn o(){ let k=7; fn i(){ return k; } }")
        k = [b for b in res.bindings if b.name == "k"][0]
        self.assertTrue(k.captured and k.boxed)
        self.assertFalse(k.mutated)

    def test_mutated_local_that_does_not_escape_is_not_boxed(self):
        res = analyze("fn o(){ let m=0; m=m+1; print(m); }")
        m = [b for b in res.bindings if b.name == "m"][0]
        self.assertTrue(m.mutated)
        self.assertFalse(m.captured)
        self.assertFalse(m.boxed)

    def test_named_functions_boxed_for_recursion(self):
        res = analyze("fn rec(n){ return rec; }")
        r = [b for b in res.bindings if b.name == "rec"][0]
        self.assertEqual(r.kind, "fn")
        self.assertTrue(r.boxed)

    def test_mutated_captured_parameter_boxed(self):
        res = analyze("fn o(s){ fn i(){ s=s+1; } }")
        s = [b for b in res.bindings if b.name == "s"][0]
        self.assertTrue(s.boxed and s.captured and s.mutated)


class TestFunctionIdentity(unittest.TestCase):
    def test_siblings_share_depth_but_have_unique_ids(self):
        res = analyze(
            "fn p(){ fn a(){} fn b(){} }"
            "fn q(){ fn c(){} }")
        fb = funcs_by_name(res)
        self.assertEqual(fb["a"].depth, fb["b"].depth)   # both depth 2
        self.assertNotEqual(fb["a"].func_id, fb["b"].func_id)
        ids = {fi.func_id for fi in res.functions}
        self.assertEqual(len(ids), len(res.functions))

    def test_owner_is_func_id_not_depth(self):
        # c is a sibling of a/b but captures a binding of q (same depth 1);
        # ownership must still distinguish p from q.
        res = analyze(
            "fn p(){ fn a(){} fn b(){} }"
            "fn q(){ let z=0; fn c(){ return z; } }")
        fb = funcs_by_name(res)
        z = [b for b in res.bindings if b.name == "z"][0]
        self.assertEqual(z.owner, fb["q"].func_id)
        self.assertEqual([b.name for b in fb["c"].free], ["z"])


class TestSlotAllocation(unittest.TestCase):
    def test_slots_unique_per_function(self):
        res = analyze("""
        fn a(p){ let u=1; let v=2; fn q(){ return p; } }
        """)
        fa = funcs_by_name(res)["a"]
        owned = [b for b in res.bindings if b.owner == fa.func_id]
        slots = [b.slot for b in owned]
        self.assertEqual(len(slots), len(set(slots)))
        self.assertEqual(fa.slots, len(owned))


if __name__ == "__main__":
    unittest.main()
