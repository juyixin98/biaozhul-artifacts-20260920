"""IR text dumper/parser round-trip."""
from __future__ import annotations

import unittest

from ssa_toolchain.ir import dump_module, Module
from ssa_toolchain.ir_parser import parse_ir
from ssa_toolchain.interp import Interpreter

from .helpers import compile_source, load_example


class RoundTripTests(unittest.TestCase):
    def _roundtrip(self, name, inputs):
        res = compile_source(load_example(name), inputs=inputs)
        for stage, mod in (("raw", res.raw_module),
                           ("ssa", res.ssa_module),
                           ("flat", res.flat_module)):
            text = dump_module(mod)
            m2 = parse_ir(text)
            self.assertEqual(
                dump_module(m2).split(),
                text.split(),
                msg=f"{name} / {stage} did not round-trip")

    def test_diamond(self):
        self._roundtrip("diamond.mini", [4])

    def test_sum_loop(self):
        self._roundtrip("sum_loop.mini", [7])

    def test_swap_loop(self):
        self._roundtrip("swap_loop.mini", [3])

    def test_unreachable(self):
        self._roundtrip("unreachable.mini", [1])

    def test_roundtripped_flat_executes(self):
        res = compile_source(load_example("sum_loop.mini"), inputs=[10])
        text = dump_module(res.flat_module)
        m2 = parse_ir(text)
        got = Interpreter(m2, "flat").run("main", [10])
        self.assertEqual(got.return_value, 55)


class ParserRejectTests(unittest.TestCase):
    def test_bad_phi_literal(self):
        text = """
        func main() {
        entry:
          %x.phi0 = phi [p 0]
          ret %x.phi0
        }
        """
        with self.assertRaises(Exception):
            parse_ir(text)

    def test_missing_block(self):
        text = """
        func main() {
        entry:
          jmp nowhere
        }
        """
        m = parse_ir(text)
        # parser is permissive about labels; the verifier catches it
        from ssa_toolchain.analysis.verify import verify_ssa
        from ssa_toolchain.errors import VerifyError
        with self.assertRaises(VerifyError):
            verify_ssa(m.funcs["main"])


if __name__ == "__main__":
    unittest.main()
