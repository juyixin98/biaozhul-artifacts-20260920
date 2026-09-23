"""SCCP 格分析核心测试：三值格、可达边联合分析、合流反例。"""

import unittest

from constprop.cfg import build_cfg
from constprop.model import UNDEF
from constprop.optimizer import optimize
from constprop.parser import parse_source
from constprop.sccp import (
    BOTTOM,
    TOP,
    LatticeValue,
    meet,
    run_sccp,
)
from constprop.source import SourceText
from constprop.ssa import construct_ssa


def sccp(text):
    src = SourceText(text)
    cfg = build_cfg(parse_source(src), src)
    construct_ssa(cfg)
    return cfg, run_sccp(cfg), src


class TestLattice(unittest.TestCase):
    T = LatticeValue.top()
    B = LatticeValue.bottom()

    def test_meet_top_is_identity(self):
        self.assertEqual(meet(self.T, LatticeValue.const(3)),
                         LatticeValue.const(3))
        self.assertEqual(meet(LatticeValue.const(3), self.T),
                         LatticeValue.const(3))

    def test_meet_same_const(self):
        self.assertEqual(meet(LatticeValue.const(7),
                              LatticeValue.const(7)),
                         LatticeValue.const(7))

    def test_meet_different_const_is_bottom(self):
        self.assertEqual(meet(LatticeValue.const(20),
                              LatticeValue.const(40)), self.B)

    def test_meet_bottom_absorbs(self):
        self.assertEqual(meet(self.B, LatticeValue.const(1)), self.B)
        self.assertEqual(meet(self.T, self.B), self.B)


class TestUndefLattice(unittest.TestCase):
    U = LatticeValue.undef()

    def test_undef_distinct_from_top(self):
        self.assertNotEqual(self.U, LatticeValue.top())

    def test_meet_top_absorbs_but_undef_does_not(self):
        # 未执行入边 top：被常量乐观吸收
        self.assertEqual(meet(LatticeValue.top(), LatticeValue.const(7)),
                         LatticeValue.const(7))
        # 显式 undef：不被常量吸收（确定的 poison）
        self.assertEqual(meet(self.U, LatticeValue.const(7)), self.U)
        self.assertEqual(meet(LatticeValue.const(7), self.U), self.U)

    def test_meet_undef_with_undef_and_bottom(self):
        self.assertEqual(meet(self.U, self.U), self.U)
        self.assertEqual(meet(self.U, LatticeValue.bottom()),
                         LatticeValue.bottom())
        self.assertEqual(meet(LatticeValue.top(), self.U), self.U)

    def test_short_circuit_undef_result_not_constant_folded(self):
        # 0 || undef => undef（右路求值得到 undef），不得折叠成常量
        text = "s = 0;\nx = (s || missing);\nprint 9;\nprint x;\n"
        _, res, _ = sccp(text)
        join = [lv for n, lv in res.lattice.items()
                if n.startswith("$j") or n.startswith("x.")]
        # 至少有一个 undef 格值传播到 x
        self.assertTrue(any(lv.kind == "undef" for lv in join))

    def test_undef_and_const_on_dynamic_edges_meet_undef(self):
        # 动态条件（k 来自回边，格为 bottom）：真边 x=undef 表达式，
        # 假边 x=1；join 的 φ = undef ∧ const(1) 必须是 undef，
        # 不能被常量吸收。x 仅向下传递，先 print 9，观察 x 时才报错。
        text = (
            "k=0;\ni=1;\nwhile(i<=2){k=k+1;i=i+1;}\n"
            "if(k){ x = missing + 1; } else { x = 1; }\n"
            "print 9;\nprint x;\n"
        )
        _, res, _ = sccp(text)
        # join φ 与其后的使用必须是 undef（x.2 = φ, x.3 = 到 print 的版本）；
        # 分支内部的局部定值 x.1=const(1) 允许存在。
        join = {n: lv for n, lv in res.lattice.items()
                if n in ("x.2", "x.3")}
        self.assertTrue(join)
        for lv in join.values():
            self.assertEqual(lv.kind, "undef")


class TestSimplePropagation(unittest.TestCase):
    def test_constant_arithmetic(self):
        _, res, _ = sccp("x = 40;\ny = x + 2;\nprint y;\n")
        # 找到 y 的最终版本
        y_consts = {n: lv for n, lv in res.lattice.items()
                    if n.startswith("y.") and lv.kind == "const"}
        self.assertTrue(any(v.value == 42 for v in y_consts.values()))

    def test_constant_condition_prunes_branch(self):
        _, res, _ = sccp(
            "if (1) { print 1; } else { print 2; }\n"
        )
        else_blocks = [b for b in res.reachable if b.startswith("else")]
        then_blocks = [b for b in res.reachable if b.startswith("then")]
        self.assertEqual(else_blocks, [])
        self.assertTrue(then_blocks)

    def test_impossible_branch_never_executable(self):
        _, res, _ = sccp("if (0) { x = 1/0; } print 9;\n")
        dead = [b for b in res.cfg.blocks if b.label not in res.reachable]
        # 不可达块的出/入边都不应是可执行边
        for (a, b) in res.executable_edges:
            self.assertNotIn(a, [d.label for d in dead])


class TestBranchesAndLoops(unittest.TestCase):
    def test_dynamic_condition_makes_both_edges_executable(self):
        # 用回边制造 ⊥ 条件
        text = ("k=0;\ni=1;\nwhile(i<=2){k=k+1;i=i+1;}\n"
                "if (k) { print 1; } else { print 2; }\n")
        _, res, _ = sccp(text)
        # k 循环出口 φ 应为 bottom
        k_vals = [lv for n, lv in res.lattice.items()
                  if n.startswith("k.")]
        self.assertIn(LatticeValue.bottom(), k_vals)
        # 动态 if 的两条边都应可执行
        cond_blocks = [b for b in res.cfg.blocks
                       if b.terminator is not None
                       and b.terminator.op == "br"
                       and any(tl.startswith("then")
                               for tl in b.terminator.blocks)]
        self.assertTrue(cond_blocks)
        br = cond_blocks[0]
        self.assertIn((br.label, br.terminator.blocks[0]),
                      res.executable_edges)
        self.assertIn((br.label, br.terminator.blocks[1]),
                      res.executable_edges)

    def test_zero_trip_loop_body_unreachable_phi_keeps_const(self):
        text = ("s=0;\ni=1;\nwhile(i>100){s=s+i;}\nprint s;\n")
        cfg, res, _ = sccp(text)
        body = next(b for b in cfg.blocks
                    if b.label.startswith("while.body"))
        self.assertNotIn(body.label, res.reachable)
        s_phi = [lv for n, lv in res.lattice.items()
                 if n.startswith("s.") and lv.kind == "const"
                 and lv.value == 0]
        self.assertTrue(s_phi)

    def test_multi_trip_loop_carried_phi_becomes_bottom(self):
        text = ("r=0;\nt=1;\nwhile(t<=3){r=r+t;t=t+1;}\nprint r;\n")
        _, res, _ = sccp(text)
        r_phis = [lv for n, lv in res.lattice.items()
                  if n.startswith("r.")]
        self.assertIn(LatticeValue.bottom(), r_phis)

    def test_constant_after_unreachable_branch(self):
        # if(0){v=500;} 之后 v 仍为活边上的常量
        text = "v=5;\nif(0){v=500;}\nprint v;\n"
        _, res, _ = sccp(text)
        v_consts = [lv.value for n, lv in res.lattice.items()
                    if n.startswith("v.") and lv.kind == "const"]
        self.assertIn(5, v_consts)
        self.assertNotIn(500, v_consts)


class TestConfluenceCounterexamples(unittest.TestCase):
    TEXT = (
        "k=0;\ni=1;\nwhile(i<=2){k=k+1;i=i+1;}\n"  # k -> bottom(运行期2)
        "if(k){y=20;}else{y=40;}\n"                  # y: 20 ∧ 40
        "if(k){z=7;}else{z=7;}\n"                    # z: 7 ∧ 7
        "print y;\nprint z;\n"
    )

    def test_different_constants_meet_to_bottom(self):
        _, res, _ = sccp(self.TEXT)
        y_join = [lv for n, lv in res.lattice.items()
                  if n.startswith("y.")]
        self.assertIn(LatticeValue.bottom(), y_join)

    def test_same_constants_stay_const(self):
        _, res, _ = sccp(self.TEXT)
        z_join = [lv for n, lv in res.lattice.items()
                  if n.startswith("z.") and lv.kind == "const"
                  and lv.value == 7]
        self.assertTrue(z_join)

    def test_correlation_same_value_branch(self):
        # Wegman 经典：即使分支条件未知，两路赋同值 => 常量
        text = ("k=0;\ni=1;\nwhile(i<=2){k=k+1;i=i+1;}\n"
                "if(k){a=7;}else{a=7;}\nprint a;\n")
        cfg, res, _ = sccp(text)
        join = next(b for b in cfg.blocks
                    if b.terminator is None or
                    (b.insts and b.insts[-1].op not in ("br",))
                    and any(x.op == "print" for x in b.insts))
        a_in_join = [lv for n, lv in res.lattice.items()
                     if n.startswith("a.") and lv.kind == "const"]
        self.assertTrue(any(lv.value == 7 for lv in a_in_join))


class TestSafetyFolding(unittest.TestCase):
    def test_constant_divisor_zero_not_folded(self):
        # b 被折叠成 const(0)，但 3/b 的结果必须是 bottom 且指令保留
        text = "b = 5 - 5;\nc = 3 / b;\nprint c;\n"
        cfg, res, src = sccp(text)
        div_targets = [
            i.target for b in cfg.blocks for i in b.insts
            if i.op == "/"
        ]
        self.assertTrue(div_targets)
        for t in div_targets:
            self.assertEqual(res.lattice[t].kind, BOTTOM)

    def test_constant_mod_zero_not_folded(self):
        text = "z = 5 % (1-1);\nprint z;\n"
        cfg, res, _ = sccp(text)
        mod_targets = [i.target for b in cfg.blocks for i in b.insts
                       if i.op == "%"]
        for t in mod_targets:
            self.assertEqual(res.lattice[t].kind, BOTTOM)

    def test_safe_division_constant_is_folded(self):
        text = "c = 20 / 4;\nprint c;\n"
        _, res, _ = sccp(text)
        c = [lv for n, lv in res.lattice.items()
             if n.startswith("c.") and lv.kind == "const"]
        self.assertTrue(any(lv.value == 5 for lv in c))

    def test_undef_use_not_in_constants(self):
        text = "x = never + 1;\n"
        _, res, _ = sccp(text)
        # 引用 undef 的结果不得成为常量；应为显式 undef 格
        for n, lv in res.lattice.items():
            if n.startswith("$t"):
                self.assertNotEqual(lv.kind, "const")
        t_lvs = [lv for n, lv in res.lattice.items() if n.startswith("$t")]
        self.assertTrue(any(lv.kind == "undef" for lv in t_lvs))


if __name__ == "__main__":
    unittest.main()
