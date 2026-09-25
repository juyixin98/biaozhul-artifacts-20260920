import unittest

from lattlang.analysis import run_sccp
from lattlang.irbuild import build_ir
from lattlang.parser import parse
from lattlang import ssa as ssa_mod


def analyze(src):
    prog = ssa_mod.build_ssa(build_ir(parse(src)))
    return run_sccp(prog), prog


class TestSCCP(unittest.TestCase):
    def test_constant_assignment(self):
        r, _ = analyze("x := 1 + 2;\nprint x;")
        x_names = {k: v for k, v in r.lat.items() if k.startswith("x.")}
        self.assertTrue(any(v.is_const() and v.value == 3
                            for v in x_names.values()))

    def test_unreachable_branch_marked_dead(self):
        r, _ = analyze("if 1 { print 1; } else { print 2; }")
        self.assertTrue(any("then" in e[1] for e in r.executable_edges))
        self.assertFalse(any("else" in e[1] for e in r.executable_edges))

    def test_zero_trip_loop_body_unreachable(self):
        r, _ = analyze("while 0 { print 1; }\nprint 2;")
        self.assertEqual([b for b in r.reachable if "while_body" in b], [])

    def test_loop_carried_value_is_bottom(self):
        r, _ = analyze("i := 0;\nwhile i < 3 { i := i + 1; }\nprint i;")
        phi_names = [n for n in r.lat if n.startswith("i.")]
        self.assertTrue(any(r.lat[n].is_bottom() for n in phi_names),
                        f"expected BOTTOM loop phi among {phi_names}")

    def test_confluence_meet_is_bottom(self):
        src = ("i := 0;\nwhile i < 1 { i := i + 1; }\n"
               "if i { x := 1; } else { x := 2; }\nprint x;")
        r, _ = analyze(src)
        merge_phis = [n for n, v in r.lat.items()
                      if n.startswith("x.") and v.is_bottom()]
        self.assertTrue(merge_phis, "merge phi of 1 and 2 must be BOTTOM")
        targets = [e[1] for e in r.executable_edges]
        self.assertTrue(any("then" in t for t in targets))
        self.assertTrue(any("else" in t for t in targets))

    def test_constant_zero_divisor_is_not_folded(self):
        r, prog = analyze("print 1 / 0;")
        for b in prog.blocks:
            for ins in b.instrs:
                if ins.kind == "binary" and ins.op == "div":
                    # /0 never gets a cell assigned -> reads as TOP, never a
                    # constant; the optimizer must therefore retain it.
                    self.assertTrue(r.value_of(ins.dest).is_top(),
                                    f"{ins.dest} must stay TOP for /0")

    def test_nonzero_constant_divisor_folds(self):
        r, _ = analyze("print 10 / 2;")
        self.assertIn(5, [v.value for v in r.lat.values() if v.is_const()])

    def test_dynamic_divisor_is_bottom(self):
        r, prog = analyze(
            "x := 0;\nwhile x < 2 { print 1 / x; x := x + 1; }")
        # the divisor x at the head phi is BOTTOM
        self.assertTrue(any(v.is_bottom() for v in r.lat.values()))

    def test_branch_on_bottom_keeps_both_edges(self):
        src = ("i := 0;\nwhile i < 1 { i := i + 1; }\n"
               "if i { print 1; } else { print 2; }")
        r, _ = analyze(src)
        targets = [e[1] for e in r.executable_edges]
        self.assertTrue(any("then" in t for t in targets))
        self.assertTrue(any("else" in t for t in targets))

    def test_back_edge_phi_remeets_when_edge_late(self):
        # Regression: an outer-loop body contains a nested loop, so the
        # outer head's back edge becomes executable only well after the
        # head's first visit.  The head phi must re-meet then (b -> BOTTOM),
        # not stay pinned to the entry value 0.
        src = ("c := 0;\nwhile c < 2 {\n  b := 2;\n  d := 0;\n"
               "  while d < 1 { print b; d := d + 1; }\n"
               "  c := c + 1;\n}\nprint b;")
        r, prog = analyze(src)
        head = next(b for b in prog.blocks if "while_head" in b.label
                    and any(n.startswith("b.") for n in b.phis))
        b_phi = next(n for n in head.phis if n.startswith("b."))
        self.assertTrue(r.lat[b_phi].is_bottom(),
                        f"{b_phi} must re-meet over the late back edge")

    def test_top_before_visit_bottom_after(self):
        # undefined (TOP) initially; a purely straight-line constant
        # program resolves everything; ensure no spurious BOTTOM
        r, prog = analyze("a := 40; b := a + 2; print b;")
        b_dests = [n for n in r.lat if n.startswith("b.")]
        self.assertTrue(all(r.lat[n].is_const() for n in b_dests))


if __name__ == "__main__":
    unittest.main()
