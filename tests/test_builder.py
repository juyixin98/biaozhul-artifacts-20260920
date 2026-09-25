"""Builder (AST -> IR) tests."""

import unittest

from taintlang.builder import build
from taintlang.config import Config
from taintlang.errors import BuildError
from taintlang.ir import (
    BinOp, Br, CallTerm, Const, Copy, Jmp, Ret, SinkInstr, SanitizeInstr,
    SourceInstr, UnknownCall,
)
from taintlang.parser import parse


def ir_of(src, config=None):
    config = config or Config()
    return build(parse(src), config, src)


def all_terminators(func_ir):
    return [b.terminator for b in func_ir.blocks if b.terminator is not None]


def all_instrs(func_ir):
    return [i for b in func_ir.blocks for i in b.instructions]


class TestBuilder(unittest.TestCase):
    def test_direct_source_sink_lowered_to_markers(self):
        prog = ir_of("func main() { var x = source(); sink(x); }")
        f = prog.function("main")
        kinds = [type(i) for i in all_instrs(f)]
        self.assertIn(SourceInstr, kinds)
        self.assertIn(SinkInstr, kinds)

    def test_user_call_is_terminator_with_continuation(self):
        src = ("func g() { return 1; }\n"
               "func main() { var x = g(); sink(x); }")
        prog = ir_of(src, Config(entry_points=("main",)))
        terms = all_terminators(prog.function("main"))
        calls = [t for t in terms if isinstance(t, CallTerm)]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].callee, "g")
        self.assertIsNotNone(calls[0].return_reg)
        # continuation block exists
        labels = {b.label for b in prog.function("main").blocks}
        self.assertIn(calls[0].cont, labels)

    def test_branch_structure(self):
        src = "func main() { var x; if (true) { x = 1; } else { x = 2; } }"
        prog = ir_of(src)
        terms = all_terminators(prog.function("main"))
        brs = [t for t in terms if isinstance(t, Br)]
        self.assertEqual(len(brs), 1)
        self.assertIsNotNone(brs[0].then_target)
        self.assertIsNotNone(brs[0].else_target)

    def test_while_loop_has_back_edge(self):
        src = ("func main() { var i = 0; "
               "while (i < 3) { i = i + 1; } }")
        prog = ir_of(src)
        f = prog.function("main")
        # head block is targeted by both entry jmp and body back-edge
        targets = []
        for t in all_terminators(f):
            if isinstance(t, Jmp):
                targets.append(t.target)
            if isinstance(t, Br):
                targets.extend([t.then_target, t.else_target])
        heads = [lbl for lbl in set(targets) if targets.count(lbl) >= 2]
        self.assertTrue(heads, "expected a loop-head block with >=2 in-edges")

    def test_undeclared_variable(self):
        with self.assertRaises(BuildError):
            ir_of("func main() { sink(undeclared); }")

    def test_duplicate_function(self):
        with self.assertRaises(BuildError):
            ir_of("func f() { return 1; } func f() { return 2; }")

    def test_function_shadows_marker(self):
        with self.assertRaises(BuildError):
            ir_of("func source() { return 1; }")

    def test_arity_mismatch(self):
        with self.assertRaises(BuildError):
            ir_of("func g(a) { return a; } "
                  "func main() { var x = g(1, 2); }")

    def test_unknown_function_becomes_opaque_instruction(self):
        prog = ir_of("func main() { var x = external(1); sink(x); }")
        f = prog.function("main")
        self.assertTrue(any(isinstance(i, UnknownCall) for i in all_instrs(f)))
        # no CallTerm should mention the unknown name
        for t in all_terminators(f):
            if isinstance(t, CallTerm):
                self.assertNotEqual(t.callee, "external")

    def test_sanitizer_marker_arity(self):
        with self.assertRaises(BuildError):
            ir_of("func main() { var x = sanitize(); }")
        with self.assertRaises(BuildError):
            ir_of("func main() { var x = source(1); }")

    def test_instruction_uids_are_global(self):
        src = ("func g() { return source(); }\n"
               "func main() { var x = g(); sink(x); }")
        prog = ir_of(src, Config(entry_points=("main",)))
        uids = [i.uid for f in prog.functions for i in all_instrs(f)]
        self.assertEqual(len(uids), len(set(uids)))

    def test_spans_slice_source_text(self):
        src = "func main() { var x = source(); }"
        prog = ir_of(src)
        src_instr = next(i for f in prog.functions for i in all_instrs(f)
                         if isinstance(i, SourceInstr))
        self.assertEqual(src_instr.span.text, "source()")

    def test_nested_call_arguments(self):
        src = ("func g(a, b) { return a; }\n"
               "func main() { var x = g(source(), 1); sink(x); }")
        prog = ir_of(src, Config(entry_points=("main",)))
        call = next(t for t in all_terminators(prog.function("main"))
                    if isinstance(t, CallTerm))
        self.assertEqual(len(call.args), 2)


if __name__ == "__main__":
    unittest.main()
