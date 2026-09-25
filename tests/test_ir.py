"""IR 构建测试：指令降级、CFG 连接、内置识别、语义检查。"""

import unittest

from taintflow.config import AnalysisConfig
from taintflow.errors import AnalysisError
from taintflow.lexer import Lexer
from taintflow.parser import Parser
from taintflow.ir import IRBuilder


def build(src, cfg=None):
    cfg = cfg or AnalysisConfig()
    prog = Parser(Lexer(src).tokenize()).parse_program()
    return IRBuilder(prog, cfg).build()


def instrs(ir_fn):
    out = []
    for label in ir_fn.order:
        for ins in ir_fn.blocks[label].instructions:
            out.append((label, ins))
    return out


def ops(ir_fn):
    return [ins.op for _, ins in instrs(ir_fn)]


class TestIR(unittest.TestCase):
    def test_source_sink_sanitize_builtins(self):
        ir = build("fn main() { x = source(); y = clean(x); sink(y); }")
        fn = ir.functions["main"]
        self.assertIn("source", ops(fn))
        self.assertIn("sink", ops(fn))
        self.assertIn("sanitize", ops(fn))

    def test_flatten_binop_to_temp(self):
        ir = build("fn main() { x = 1 + 2 * 3; }")
        fn = ir.functions["main"]
        binops = [ins for _, ins in instrs(fn) if ins.op == "binop"]
        self.assertEqual(len(binops), 2)
        self.assertTrue(all(ins.dst.startswith("__t") for ins in binops))

    def test_if_cfg_branches_join(self):
        ir = build("fn main() { if (c) { a = 1; } else { b = 2; } return 0; }")
        fn = ir.functions["main"]
        brs = [ins for _, ins in instrs(fn) if ins.op == "br"]
        self.assertEqual(len(brs), 1)
        self.assertEqual(len(brs[0].targets), 2)
        # then 与 else 都应跳到同一个 join
        then_lbl, else_lbl = brs[0].targets
        self.assertEqual(fn.blocks[then_lbl].successors,
                         fn.blocks[else_lbl].successors)

    def test_while_cfg_back_edge(self):
        ir = build("fn main() { while (c) { c = 0; } return 0; }")
        fn = ir.functions["main"]
        jumps = [ins for _, ins in instrs(fn) if ins.op == "jump"]
        targets = [ins.targets[0] for ins in jumps]
        # 存在跳回 head 的回边
        heads = [lbl for lbl in fn.order if lbl.startswith("while_head")]
        self.assertTrue(any(t in heads for t in targets))

    def test_user_call_instruction(self):
        ir = build("fn id(x){ return x; } fn main(){ sink(id(source())); }")
        call = next(ins for _, ins in instrs(ir.functions["main"])
                    if ins.op == "call" and ins.call_name == "id")
        self.assertTrue(call.dst.startswith("__t"))

    def test_arity_check(self):
        with self.assertRaises(AnalysisError):
            build("fn f(a){ return a; } fn main(){ f(1,2); }")

    def test_builtin_name_collision(self):
        with self.assertRaises(AnalysisError):
            build("fn source() { return 0; }")

    def test_duplicate_function(self):
        with self.assertRaises(AnalysisError):
            build("fn f(){} fn f(){}")

    def test_implicit_return_inserted(self):
        ir = build("fn f(){ x = 1; }")
        last = ir.functions["f"].blocks[ir.functions["f"].order[-1]].instructions[-1]
        self.assertEqual(last.op, "ret")
        self.assertEqual(last.operands, ())

    def test_unknown_call_warning_conservative(self):
        ir = build("fn main(){ y = mystery(source()); sink(y); }")
        self.assertTrue(any(w["kind"] == "unknown_call" for w in ir.warnings))

    def test_unknown_call_strict_mode_error(self):
        cfg = AnalysisConfig.from_dict({"conservative_unknown_calls": False})
        with self.assertRaises(AnalysisError):
            build("fn main(){ mystery(1); }", cfg)

    def test_instructions_retain_spans(self):
        ir = build("fn main() {\nx = source();\n}")
        fn = ir.functions["main"]
        src = next(ins for _, ins in instrs(fn) if ins.op == "source")
        self.assertEqual(src.span.start.line, 2)

    def test_custom_builtin_names(self):
        cfg = AnalysisConfig.from_dict({
            "sources": ["getInput"],
            "sinks": ["log"],
            "sanitizers": ["escape"],
        })
        ir = build("fn main(){ x = getInput(); log(escape(x)); }", cfg)
        fn = ir.functions["main"]
        self.assertIn("source", ops(fn))
        self.assertIn("sink", ops(fn))
        self.assertIn("sanitize", ops(fn))

    def test_uninitialized_read_warning(self):
        ir = build("fn main(){ sink(z); }")
        self.assertTrue(any(w["kind"] == "uninitialized_read" for w in ir.warnings))

    def test_assigned_in_branch_not_flagged(self):
        # 在分支中赋值再使用：不报“从未赋值”（跨分支的保守检查）
        ir = build("fn main(){ c = 1; if (c) { z = 1; } sink(z); }")
        self.assertFalse(any(w["kind"] == "uninitialized_read" for w in ir.warnings))

    def test_uninitialized_condition_is_flagged(self):
        # 条件里直接读未初始化变量：仍然要报
        ir = build("fn main(){ if (c) { z = 1; } }")
        self.assertTrue(any(w["kind"] == "uninitialized_read" for w in ir.warnings))


if __name__ == "__main__":
    unittest.main()
