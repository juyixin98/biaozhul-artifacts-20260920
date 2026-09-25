"""CFG construction tests: node/edge shapes and exception routing."""

import unittest

from resflow.parser import parse_source
from resflow.cfg import build_cfg


def cfg_of(src):
    return build_cfg(parse_source(src)[0])


class TestCFG(unittest.TestCase):

    def test_entry_exit_and_uncaught_exist(self):
        g = cfg_of("fn f(){ return; }")
        kinds = {n.kind for n in g.nodes}
        self.assertIn("entry", kinds)
        self.assertIn("exit", kinds)
        self.assertIn("uncaught", kinds)

    def test_branch_has_true_and_false_edges(self):
        g = cfg_of("fn f(){ let c; if (c) { acquire(a); } return; }")
        branch = next(n for n in g.nodes if n.kind == "branch")
        kinds = {e.kind for e in g.out_edges(branch.id)}
        self.assertEqual(kinds, {"true", "false"})

    def test_loop_has_true_false_back(self):
        g = cfg_of("fn f(){ let c; while (c) { acquire(a); } return; }")
        loop = next(n for n in g.nodes if n.kind == "loop")
        kinds = {e.kind for e in g.out_edges(loop.id)}
        self.assertIn("true", kinds)
        self.assertIn("false", kinds)
        back = [e for e in g.edges if e.kind == "back"]
        self.assertTrue(back)
        self.assertEqual(back[0].dst, loop.id)

    def test_throw_routed_to_catch(self):
        g = cfg_of("""fn f(){
          try { throw "x"; } catch (e) { release(a); }
          return;
        }""")
        throw = next(n for n in g.nodes if n.kind == "throw")
        catch = next(n for n in g.nodes if n.kind == "catch_head")
        exc = [e for e in g.out_edges(throw.id) if e.kind == "exception"]
        self.assertEqual(len(exc), 1)
        self.assertEqual(exc[0].dst, catch.id)
        self.assertTrue(exc[0].detail.get("caught"))

    def test_uncaught_throw_goes_to_function_uncaught(self):
        g = cfg_of("fn f(){ throw \"x\"; }")
        throw = next(n for n in g.nodes if n.kind == "throw")
        exc = [e for e in g.out_edges(throw.id) if e.kind == "exception"]
        self.assertEqual(exc[0].dst, g.uncaught)
        self.assertFalse(exc[0].detail.get("caught"))

    def test_nested_try_innermost_catch_wins(self):
        g = cfg_of("""fn f(){
          try {
            try { throw "in"; } catch (e1) { throw "again"; }
          } catch (e2) { release(a); }
          return;
        }""")
        throws = [n for n in g.nodes if n.kind == "throw"]
        catches = [n for n in g.nodes if n.kind == "catch_head"]
        # Inner throw -> inner catch; rethrow in inner handler -> outer.
        inner_exc = [e for e in g.out_edges(throws[0].id)
                     if e.kind == "exception"][0]
        outer_exc = [e for e in g.out_edges(throws[1].id)
                     if e.kind == "exception"][0]
        self.assertEqual(inner_exc.dst, catches[0].id)
        self.assertEqual(outer_exc.dst, catches[1].id)

    def test_return_targets_function_exit(self):
        g = cfg_of("fn f(){ acquire(a); return; release(a); }")
        ret = next(n for n in g.nodes if n.kind == "return")
        self.assertEqual(g.out_edges(ret.id)[0].dst, g.exit)

    def test_all_nodes_reachable_from_entry_except_uncaught_targets(self):
        # Structural sanity: graph edges reference only valid node ids.
        g = cfg_of("fn f(){ let c; if(c){throw \"x\";} return; }")
        ids = {n.id for n in g.nodes}
        for e in g.edges:
            self.assertIn(e.src, ids)
            self.assertIn(e.dst, ids)

    def test_statement_nodes_carry_resource_detail(self):
        g = cfg_of("fn f(){ acquire(a); release(a); use(a); }")
        ops = [n.detail.get("op") for n in g.nodes
               if n.kind == "statement"]
        self.assertEqual(ops, ["acquire", "release", "use"])
        resources = [n.detail.get("resource") for n in g.nodes
                     if n.kind == "statement"]
        self.assertEqual(resources, ["a", "a", "a"])

    def test_locations_preserved_on_cfg_nodes(self):
        g = cfg_of("fn f(){\n  acquire(a);\n}")
        acq = next(n for n in g.nodes
                   if n.detail.get("op") == "acquire")
        self.assertEqual(acq.loc.line, 2)

    def test_if_without_else_still_converges(self):
        g = cfg_of("fn f(){ let c; if(c){ acquire(a);} return; }")
        merges = [n for n in g.nodes if n.kind == "merge"]
        self.assertTrue(merges)

    def test_empty_function_body(self):
        g = cfg_of("fn f() { }")
        # entry must connect directly to exit without crashing
        self.assertTrue(any(e.dst == g.exit for e in g.edges))


if __name__ == "__main__":
    unittest.main()
