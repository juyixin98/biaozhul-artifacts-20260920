"""CFG / SSA 构造测试。"""

import unittest

from constprop.cfg import build_cfg
from constprop.parser import parse_source
from constprop.source import SourceText
from constprop.ssa import (
    compute_idom,
    construct_ssa,
    dominance_frontiers,
    _reverse_postorder,
)


def build(text):
    src = SourceText(text)
    prog = parse_source(src)
    cfg = build_cfg(prog, src)
    return cfg, src


def build_ssa(text):
    cfg, src = build(text)
    construct_ssa(cfg)
    return cfg, src


def targets(cfg):
    out = []
    for b in cfg.blocks:
        for i in b.insts:
            if i.target:
                out.append(i.target)
    return out


class TestCFG(unittest.TestCase):
    def test_linear_program_blocks(self):
        cfg, _ = build("x = 1;\nprint x;\n")
        self.assertEqual(len(cfg.blocks), 1)
        self.assertEqual(cfg.entry, "entry")
        self.assertEqual(cfg.blocks[0].terminator.op, "exit")

    def test_if_creates_branch_and_join(self):
        cfg, _ = build("if (c) { print 1; } else { print 2; }\n")
        # entry, then, else, endif
        labels = {b.label for b in cfg.blocks}
        self.assertEqual(len(labels), 4)
        entry = cfg.block("entry")
        self.assertEqual(entry.terminator.op, "br")
        self.assertEqual(len(entry.succs), 2)

    def test_while_loop_edges(self):
        cfg, _ = build("while (c) { c = 0; }\n")
        labels = {b.label for b in cfg.blocks}
        self.assertIn("while.body", " ".join(labels))
        # 找到 cond 与 body，确认回边
        cond = [b for b in cfg.blocks if b.label.startswith("while.cond")][0]
        body = [b for b in cfg.blocks if b.label.startswith("while.body")][0]
        self.assertIn(body.label, cond.succs)
        self.assertIn(cond.label, body.succs)

    def test_short_circuit_lowered_to_branches(self):
        cfg, _ = build("x = a && b;\n")
        # 至少产生 log.eval / log.short / log.join 块
        labels = " ".join(b.label for b in cfg.blocks)
        self.assertIn("log.eval", labels)
        self.assertIn("log.short", labels)
        self.assertIn("log.join", labels)


class TestDominance(unittest.TestCase):
    def test_idom_diamond(self):
        cfg, _ = build("if (c) { a = 1; } else { a = 2; }\nprint a;\n")
        idom = compute_idom(cfg)
        self.assertEqual(idom["entry"], "entry")
        # 所有块的 idom 要么是 entry 要么是其支配者
        for b in cfg.blocks:
            if b.label != "entry":
                self.assertIsNotNone(idom[b.label])

    def test_rpo_starts_at_entry(self):
        cfg, _ = build("if (c) {a=1;} else {a=2;}\nwhile (d) {d=0;}\n")
        rpo = _reverse_postorder(cfg)
        self.assertEqual(rpo[0], "entry")

    def test_dominance_frontier_of_if_preds_is_join(self):
        cfg, _ = build("if (c) { a = 1; } else { a = 2; }\nprint a;\n")
        idom = compute_idom(cfg)
        df = dominance_frontiers(cfg, idom)
        then_lbl = next(b.label for b in cfg.blocks
                        if b.label.startswith("then"))
        join_lbl = next(b.label for b in cfg.blocks
                        if b.label.startswith("endif"))
        self.assertIn(join_lbl, df[then_lbl])


class TestSSA(unittest.TestCase):
    def test_each_name_defined_once(self):
        cfg, _ = build_ssa("x = 1;\nx = x + 1;\nprint x;\n")
        names = targets(cfg)
        self.assertEqual(len(names), len(set(names)))

    def test_phi_inserted_after_if_converge(self):
        cfg, _ = build_ssa("if (c) { a = 1; } else { a = 2; }\nprint a;\n")
        join = next(b for b in cfg.blocks
                    if b.label.startswith("endif"))
        phis = [i for i in join.insts if i.is_phi]
        a_phis = [i for i in phis if (i.debug_name or "") == "a"]
        self.assertEqual(len(a_phis), 1)
        self.assertEqual(len(a_phis[0].phi_args), 2)

    def test_phi_inserted_at_loop_header(self):
        cfg, _ = build_ssa("i = 0;\nwhile (i < 3) { i = i + 1; }\n")
        cond = next(b for b in cfg.blocks
                    if b.label.startswith("while.cond"))
        phis = [i for i in cond.insts if i.is_phi]
        i_phis = [i for i in phis if (i.debug_name or "") == "i"]
        self.assertEqual(len(i_phis), 1)
        # 循环头 φ 有两个入边：入口与回边
        self.assertEqual(len(i_phis[0].phi_args), 2)

    def test_no_phi_for_simple_temp_in_loop_condition(self):
        # 回归测试：循环条件的比较临时值不应得到错误 φ
        cfg, _ = build_ssa("k=0;\ni=1;\nwhile (i<=2){k=k+1;i=i+1;}\n")
        cond = next(b for b in cfg.blocks
                    if b.label.startswith("while.cond"))
        phi_targets = {i.target for i in cond.insts if i.is_phi}
        # 临时值（$t 开头）不应有 φ
        self.assertFalse(any(t.startswith("$t") for t in phi_targets))

    def test_undefined_use_becomes_undef_sentinel(self):
        from constprop.model import UNDEF
        cfg, _ = build_ssa("x = never + 1;\n")
        found = False
        for b in cfg.blocks:
            for i in b.insts:
                if UNDEF in i.operands:
                    found = True
        self.assertTrue(found)

    def test_ssa_operands_all_resolvable_names(self):
        # 每个操作数若为字符串，要么是 UNDEF，要么在某处有定义
        from constprop.model import UNDEF
        cfg, _ = build_ssa(
            "a=1;\nif(a){b=2;}else{b=3;}\n"
            "while(b){a=a-1;b=b-1;}\nprint a;\n"
        )
        defined = set(targets(cfg))
        for b in cfg.blocks:
            for i in b.insts:
                for o in i.operands:
                    if isinstance(o, str) and o != UNDEF:
                        self.assertIn(o, defined)
                for arg in i.phi_args:
                    if isinstance(arg.value, str) and arg.value != UNDEF:
                        self.assertIn(arg.value, defined)


if __name__ == "__main__":
    unittest.main()
