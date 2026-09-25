import unittest

from lattlang.ir import Imm, print_ir, parse_ir
from lattlang.irbuild import build_ir
from lattlang.parser import parse
from lattlang.ssa import (
    build_ssa,
    compute_dominators,
    dominance_frontiers,
    immediate_dominators,
)
from lattlang.ir import index_prog


def names_in(prog):
    defined = set()
    for b in prog.blocks:
        defined.update(b.phis.keys())
        for ins in b.instrs:
            if ins.dest is not None:
                defined.add(ins.dest)
    return defined


class TestSSA(unittest.TestCase):
    def _ssa(self, src):
        cfg = build_ir(parse(src))
        return build_ssa(cfg), cfg

    def test_each_defined_name_unique(self):
        prog, _ = self._ssa(
            "i := 0;\nwhile i < 3 { print i; i := i + 1; }\nprint i;")
        seen = set()
        for b in prog.blocks:
            for d in b.phis:
                self.assertNotIn(d, seen)
                seen.add(d)
            for ins in b.instrs:
                if ins.dest is not None:
                    self.assertNotIn(ins.dest, seen)
                    seen.add(ins.dest)

    def test_phi_at_loop_head(self):
        prog, _ = self._ssa("i := 0;\nwhile i < 2 { i := i + 1; }")
        head = next(b for b in prog.blocks if "while_head" in b.label)
        self.assertTrue(head.phis)
        self.assertIn("i", [d.split(".")[0] for d in head.phis])
        # phi arity matches predecessor count
        for args in head.phis.values():
            self.assertEqual(len(args), len(head.preds))

    def test_phi_at_if_merge(self):
        prog, _ = self._ssa(
            "if 1 { x := 1; } else { x := 2; }\nprint x;")
        merge = next(b for b in prog.blocks
                     if any(d.startswith("x.") for d in b.phis))
        self.assertTrue(merge.phis)

    def test_no_phi_for_single_assignment(self):
        prog, _ = self._ssa("x := 1;\nprint x;")
        for b in prog.blocks:
            self.assertFalse(b.phis)

    def test_implicit_zero_init_via_phi(self):
        # Variable used before assignment on one merge edge reads zero.
        prog, _ = self._ssa(
            "if 1 { x := 1; } else { print 0; }\nprint x;")
        merge = next(b for b in prog.blocks
                     if any(d.startswith("x.") for d in b.phis))
        args = list(merge.phis.values())[0]
        # at least one phi arg is the immediate zero (else-branch path)
        self.assertTrue(any(isinstance(a, Imm) and a.value == 0 for a in args))

    def test_dominators_diamond(self):
        cfg = build_ir(parse("if 1 { print 1; } else { print 2; }"))
        index_prog(cfg)
        dom = compute_dominators(cfg)
        idom = immediate_dominators(dom)
        # exit's idom is the entry (merge is dominated by entry)
        self.assertEqual(idom[cfg.entry], None)
        for b in cfg.blocks:
            if b.label != cfg.entry:
                self.assertIn(cfg.entry, dom[b.label])

    def test_dominance_frontiers_loop(self):
        cfg = build_ir(parse("i := 0;\nwhile i < 2 { i := i + 1; }"))
        index_prog(cfg)
        idom = immediate_dominators(compute_dominators(cfg))
        df = dominance_frontiers(cfg, idom)
        head = next(l for l in df if "while_head" in l)
        self.assertIn(head, df[head])  # head is in its own DF


class TestIRRoundTrip(unittest.TestCase):
    def test_print_parse_round_trip(self):
        cfg = build_ir(parse(
            "x := 3;\nif x { print x; } else { print 0; }\nprint x - 1;"))
        ssa_prog = build_ssa(cfg)
        text = print_ir(ssa_prog)
        reparsed = parse_ir(text)
        self.assertEqual([b.label for b in reparsed.blocks],
                         [b.label for b in ssa_prog.blocks])
        # location suffixes survive the round trip
        for orig_b, new_b in zip(ssa_prog.blocks, reparsed.blocks):
            for oi, ni in zip(orig_b.instrs, new_b.instrs):
                self.assertEqual(oi.span.offset, ni.span.offset)

    def test_parse_ir_rejects_bad_phi_arity(self):
        # entry has two predecessors (a, b) but phi lists a single arg
        text = ("a:\n  jmp entry\n"
                "b:\n  jmp entry\n"
                "entry:\n  x.1 = phi [0]\n  ret\n")
        from lattlang.errors import LangError
        with self.assertRaises(LangError):
            parse_ir(text, entry="entry")


if __name__ == "__main__":
    unittest.main()
