"""优化器测试：改写性质、副作用/除零保护，以及差分模糊测试。"""

import random
import unittest

from constprop.cfg import build_cfg
from constprop.interp import run_ast
from constprop.ir_interp import run_ir
from constprop.optimizer import optimize
from constprop.parser import parse_source
from constprop.source import SourceText
from constprop.ssa import construct_ssa


def compile_ssa(text):
    src = SourceText(text)
    cfg = build_cfg(parse_source(src), src)
    construct_ssa(cfg)
    return cfg, src


def before_after(text, limit=2_000_000):
    src = SourceText(text)
    prog = parse_source(src)
    cfg = build_cfg(prog, src)
    construct_ssa(cfg)
    ast = run_ast(prog, src, step_limit=limit)
    before = run_ir(cfg, src, step_limit=limit)
    _, report = optimize(cfg)
    after = run_ir(cfg, src, step_limit=limit)
    return ast, before, after, report, cfg


class TestOptimizationRewrites(unittest.TestCase):
    def test_branch_simplified_to_jmp(self):
        _, _, _, report, cfg = before_after(
            "if (1) { print 1; } else { print 2; }\n"
        )
        self.assertTrue(report.simplified_branches)
        # entry 终结符应为 jmp
        self.assertEqual(cfg.block("entry").terminator.op, "jmp")
        # else 块被删除
        self.assertFalse(any(b.label.startswith("else")
                             for b in cfg.blocks))

    def test_constant_materialized_in_print(self):
        _, _, _, report, cfg = before_after("x = 42;\nprint x;\n")
        self.assertTrue(any(n.startswith("x.") for n in report.materialized))
        # print 操作数变为字面量
        prints = [i for b in cfg.blocks for i in b.insts if i.op == "print"]
        self.assertTrue(any(i.operands[0] == 42 for i in prints))

    def test_phi_single_pred_becomes_copy_then_dce(self):
        # 常量分支裁剪后，join φ 单入边，随后无使用即 DCE；程序等价
        _, _, after, report, _ = before_after(
            "if (1) { z = 1; print z; } print 9;\n"
        )
        self.assertEqual(after.output, [1, 9])


class TestSideEffectAndFaultPreservation(unittest.TestCase):
    def test_reachable_print_never_removed(self):
        # print 参数是常量，指令本身必须保留
        _, _, after, _, cfg = before_after("x=3;\nx=x+4;\nprint x;\n")
        n_print = sum(1 for b in cfg.blocks for i in b.insts
                      if i.op == "print")
        self.assertEqual(n_print, 1)
        self.assertEqual(after.output, [7])

    def test_dead_unused_divzero_is_preserved(self):
        # 结果 z 未被使用，但 5/0 必须保留并在运行期抛错
        ast, before, after, _, cfg = before_after(
            "print 9;\nz = 5 / 0;\nprint 10;\n"
        )
        divs = [i for b in cfg.blocks for i in b.insts if i.op == "/"]
        self.assertEqual(len(divs), 1, "未使用的除零指令不得被 DCE 删除")
        for r in (ast, before, after):
            self.assertEqual(r.error_code, "division-by-zero")
            self.assertEqual(r.output, [9])

    def test_dead_unused_modzero_is_preserved(self):
        ast, _, after, _, cfg = before_after(
            "print 1;\nz = 5 % 0;\nprint 2;\n"
        )
        self.assertTrue(any(i.op == "%" for b in cfg.blocks
                            for i in b.insts))
        for r in (ast, after):
            self.assertEqual(r.error_code, "division-by-zero")
            self.assertEqual(r.output, [1])

    def test_unreachable_divzero_legitimately_removed(self):
        # 不可达块内的除零：随块删除合法（运行期本来不可观察）
        ast, before, after, _, _ = before_after(
            "if (1) { print 1; } else { print 2; z = 7/0; print z; }\n"
            "print 3;\n"
        )
        for r in (ast, before, after):
            self.assertTrue(r.ok)
            self.assertEqual(r.output, [1, 3])

    def test_undef_use_preserved(self):
        ast, before, after, _, _ = before_after(
            "print 1;\na = never + 1;\nprint a;\n"
        )
        for r in (ast, before, after):
            self.assertEqual(r.error_code, "undefined-variable")
            self.assertEqual(r.output, [1])

    def test_error_location_line_matches(self):
        text = "print 1;\nb=0;\nc=3/b;\nprint c;\n"
        ast, _, after, _, _ = before_after(text)
        self.assertEqual(ast.location[0], after.location[0])


# ---------------- 差分模糊测试 ----------------

OPS_BIN = ["+", "-", "*", "/", "%", "==", "!=", "<", ">"]


def gen_program(rng, n_stmts=40):
    """生成结构良好、终止的随机 L0 程序（变量取自小集合以便合流）。"""
    lines = ["s = 0;"]
    vars_ = ["s", "a", "b", "c", "i"]

    def atom():
        if rng.random() < 0.15:
            return f"(-{rng.randint(0, 5)})"
        return rng.choice(vars_) if rng.random() < 0.6 else str(rng.randint(0, 5))

    def expr(depth=0):
        if depth >= 2 or rng.random() < 0.4:
            return atom()
        kind = rng.random()
        if kind < 0.7:
            op = rng.choice(OPS_BIN)
            return f"({expr(depth+1)} {op} {expr(depth+1)})"
        # 短路逻辑（右操作数可能含除零，以测试短路语义）
        op = rng.choice(["&&", "||"])
        return f"({expr(depth+1)} {op} {expr(depth+1)})"

    for _ in range(n_stmts):
        kind = rng.random()
        if kind < 0.4:
            lines.append(f"{rng.choice(vars_)} = {expr()};")
        elif kind < 0.62:
            lines.append(f"print {expr()};")
        elif kind < 0.8:
            cond = expr()
            # 嵌套 if：约 1/4 概率体里再放一条 print/赋值
            if rng.random() < 0.3:
                lines.append(f"if ({cond}) {{ print 1; s = s + 1; }} "
                             f"else {{ print 0; }}")
            else:
                lines.append(f"if ({cond}) {{ print 1; }} else {{ print 0; }}")
        else:
            # 有界计数循环，保证终止
            lines.append("i = 0;")
            lines.append("while (i < 3) {")
            lines.append(f"  s = s + (i {rng.choice(['+','-','*'])} 1);")
            lines.append("  i = i + 1;")
            lines.append("}")
    return "\n".join(lines) + "\n"


class TestDifferentialFuzz(unittest.TestCase):
    def test_many_random_programs_preserve_behavior(self):
        rng = random.Random(20260923)
        trials = 1000
        mismatches = []
        for t in range(trials):
            text = gen_program(rng)
            try:
                ast, before, after, _, _ = before_after(text, limit=500_000)
            except Exception as e:  # 生成器不应导致工具链崩溃
                mismatches.append((t, "crash", repr(e), text))
                continue
            if ast.signature() != before.signature():
                mismatches.append((t, "AST!=IR-before",
                                   (ast.signature(), before.signature()), text))
            elif before.signature() != after.signature():
                mismatches.append((t, "before!=after",
                                   (before.signature(), after.signature()), text))
        if mismatches:
            t, kind, detail, text = mismatches[0]
            self.fail(
                f"{len(mismatches)}/{trials} mismatches; first: "
                f"trial={t} {kind}\n{detail}\n--- program ---\n{text}"
            )


if __name__ == "__main__":
    unittest.main()
