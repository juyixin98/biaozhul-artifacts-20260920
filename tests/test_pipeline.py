"""端到端：raw / ssa / exec 三形态解释执行等价 + 关键边分裂 + 整数语义。"""

import glob
import os
import unittest

from ssa_tool.interpreter import RuntimeExecError, interpret, trunc_div, trunc_mod
from ssa_tool.ir import dump_function
from ssa_tool.ir_builder import build_ir
from ssa_tool.parser import parse
from ssa_tool.phi_elimination import eliminate_phis
from ssa_tool.ssa_construction import construct_ssa
from ssa_tool.ssa_validate import validate_ssa

EX_DIR = os.path.join(os.path.dirname(__file__), "..", "examples")


def three_flavors(src):
    raw = build_ir(parse(src, "<test>"))
    ssa = construct_ssa(raw.copy())
    validate_ssa(ssa)
    exec_fn = eliminate_phis(ssa.copy())
    return raw, ssa, exec_fn


PROGRAMS = {
    "diamond_gt0": ("""
func main(a) { var x; if (a > 0) {x=10;} else {x=20;} return x + a; }
""", {4: 14, -3: 17, 0: 20}),
    "loop_sum": ("""
func main(n) { var s=0; var i=1; while(i<=n){ s=s+i; i=i+1; } return s; }
""", {0: 0, 1: 1, 5: 15, 10: 55, 100: 5050}),
    "nested": ("""
func main(a,b){
  var x=0;
  if (a>0){ if (b>0){x=1;} else {x=2;} } else { x=3; }
  return x;
}
""", {(7, 7): 1, (7, -1): 2, (-1, 7): 3, (-1, -1): 3}),
    "while_break_like_return": ("""
func main(n) {
  var i=0;
  while (i < 100) {
    if (i == n) { return i; }
    i = i + 1;
  }
  return -1;
}
""", {0: 0, 7: 7, 99: 99, 100: -1, 500: -1}),
    "nested_loops": ("""
func main(n) {
  var c=0; var i=0;
  while (i < n) {
    var j=0;
    while (j < n) { c = c + 1; j = j + 1; }
    i = i + 1;
  }
  return c;
}
""", {0: 0, 1: 1, 4: 16, 7: 49}),
    "mod_div": ("""
func main(a,b){ return (a / b) * 100 + (a % b); }
""", {(17, 5): 302, (-17, 5): -302, (17, -5): -298, (-17, -5): 298}),
    "short_circuit": ("""
func main(a,b){
  var x=0;
  if (a > 5 && b > 5) { x = 100; }
  if (a < 0 || b < 0) { x = x + 7; }
  return x;
}
""", {(6, 6): 100, (6, 0): 0, (-1, 6): 7, (6, -1): 7, (-1, -1): 7}),
}


class TestEndToEnd(unittest.TestCase):
    def _check_all(self, src, args, want):
        raw, ssa, exec_fn = three_flavors(src)
        rr = interpret(raw, list(args) if isinstance(args, tuple) else [args])
        sr = interpret(ssa, list(args) if isinstance(args, tuple) else [args])
        er = interpret(exec_fn, list(args) if isinstance(args, tuple) else [args])
        self.assertEqual(rr.value, want, msg=f"raw 结果错误 args={args}")
        self.assertEqual(sr.value, want, msg=f"ssa 结果错误 args={args}")
        self.assertEqual(er.value, want, msg=f"exec 结果错误 args={args}")

    def test_programs(self):
        for name, (src, table) in PROGRAMS.items():
            for args, want in table.items():
                with self.subTest(name=name, args=args):
                    self._check_all(src, args, want)

    def test_example_files(self):
        expected = {
            "diamond.toy": [([4], 14), ([-3], 17)],
            "loop_sum.toy": [([10], 55), ([0], 0), ([100], 5050)],
            "unreachable.toy": [([], 5)],
            "unreachable2.toy": [([1], 1), ([-5], 4)],
            "nested_branch.toy": [([7, 7], 101), ([-1, 7], 3)],
            "swap_loop.toy": [([0], 3), ([1], 3), ([2], 3), ([5], 3)],
        }
        for fname, cases in expected.items():
            path = os.path.join(EX_DIR, fname)
            with open(path, encoding="utf-8") as f:
                raw = build_ir(parse(f.read(), path))
            ssa = construct_ssa(raw.copy())
            validate_ssa(ssa)
            exec_fn = eliminate_phis(ssa.copy())
            for args, want in cases:
                self.assertEqual(interpret(raw, args).value, want,
                                 msg=(fname, args))
                self.assertEqual(interpret(ssa, args).value, want)
                self.assertEqual(interpret(exec_fn, args).value, want)

    def test_critical_edge_split_exists(self):
        src = PROGRAMS["nested"][0]
        _, _, exec_fn = three_flavors(src)
        splits = [b.name for b in exec_fn.blocks if b.name.startswith("split.")]
        # 内层 merge -> 外层 merge 是关键边（源出度1，实际不出度>1？）
        # 本程序内层 merge 出度 1，不是关键边；但短路或多分支程序里存在。
        # 改用短路 && 的汇合边验证
        src2 = PROGRAMS["short_circuit"][0]
        _, _, ex2 = three_flavors(src2)
        splits2 = [b for b in ex2.blocks if b.name.startswith("split.")]
        self.assertTrue(splits2, dump_function(ex2))
        for b in splits2:
            # split 块：单前驱单后继，内含 copy
            self.assertEqual(len(b.successors()), 1)
            self.assertTrue(b.instrs)

    def test_no_phis_after_elimination(self):
        for name, (src, _) in PROGRAMS.items():
            _, _, ex = three_flavors(src)
            self.assertFalse(any(b.phis for b in ex.blocks), msg=name)

    def test_exec_no_memory_or_phi(self):
        for name, (src, _) in PROGRAMS.items():
            _, _, ex = three_flavors(src)
            for b in ex.blocks:
                for ins in b.instrs:
                    self.assertNotIn(ins.op, ("alloc", "load", "store"))

    def test_exec_cfg_consistency(self):
        for name, (src, _) in PROGRAMS.items():
            _, _, ex = three_flavors(src)
            labels = {b.name for b in ex.blocks}
            preds = ex.preds()
            for b in ex.blocks:
                for s in b.successors():
                    self.assertIn(s, labels)
                    self.assertIn(b.name, preds[s])

    def test_division_by_zero_reports_source_line(self):
        src = "func main(a){\n  return 1 / a;\n}\n"
        raw, ssa, ex = three_flavors(src)
        for fn in (raw, ssa, ex):
            with self.assertRaises(RuntimeExecError) as cm:
                interpret(fn, [0])
            self.assertIn("除以零", str(cm.exception))
            self.assertEqual(cm.exception.line, 2)

    def test_infinite_loop_guard(self):
        src = "func main(){ while(1){} return 0; }"
        raw, _, _ = three_flavors(src)
        with self.assertRaises(RuntimeExecError):
            interpret(raw, [], max_steps=1000)

    def test_unreachable_not_executed(self):
        src = "func main(){ if(1){ return 5; } return 9; }"
        raw, ssa, ex = three_flavors(src)
        for fn in (raw, ssa, ex):
            self.assertEqual(interpret(fn).value, 5)

    def test_truncation_semantics(self):
        self.assertEqual(trunc_div(-17, 5), -3)
        self.assertEqual(trunc_mod(-17, 5), -2)
        self.assertEqual(trunc_div(17, -5), -3)
        self.assertEqual(trunc_mod(17, -5), 2)


if __name__ == "__main__":
    unittest.main()
