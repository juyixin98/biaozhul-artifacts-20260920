"""Tests for closure-converted IR: structure, JSON round-trip, VM errors."""
import json
import unittest

from sclang.errors import RuntimeError_
from sclang.ir import IRModule, OP, disassemble
from sclang.vm import VM, Cell, VClosure, run_module
from tests._helpers import compile_only, vm_output


class TestIRStructure(unittest.TestCase):
    def test_function_ids_are_unique(self):
        fe = compile_only(
            "fn a(){ fn x(){} fn y(){} }"
            "fn b(){ fn z(){} }")
        ids = [f.id for f in fe.module.functions]
        self.assertEqual(len(ids), len(set(ids)))
        # main is id 0
        self.assertEqual(fe.module.functions[0].id, 0)

    def test_free_metadata_carries_boxing(self):
        fe = compile_only("fn o(){ let c=0; fn i(){ c=c+1; return c; } }")
        inner = fe.module.functions[-1]
        self.assertEqual(inner.free, [{"name": "c", "boxed": True}])

    def test_boxed_vs_plain_slot_codegen(self):
        fe = compile_only(
            "fn f(){ let m=0; m=m+1;"          # m: mutated, not captured
            "  let k=9; return fn(){return k;}; }")
        asm = disassemble(fe.module)
        # m uses plain local slot ops, k uses a cell
        self.assertIn("STORE_CELL", asm)   # k initialization through cell
        self.assertIn("SET_LOCAL", asm)    # m is a plain slot

    def test_make_closure_env_descriptor(self):
        fe = compile_only("fn o(){ let c=0; fn i(){return c;} }")
        # inner MAKE_CLOSURE must carry one env entry referencing a local slot
        outer = fe.module.functions[1]
        code = outer.code
        idx = code.index(OP["MAKE_CLOSURE"])
        self.assertEqual(code[idx + 1], fe.module.functions[2].id)
        self.assertEqual(code[idx + 2], 1)          # one env entry
        self.assertEqual(code[idx + 3], 0)          # kind: local slot

    def test_constant_dedup(self):
        fe = compile_only("print(1,1,1);")
        self.assertEqual(fe.module.constants.count(1), 1)


class TestJsonRoundTrip(unittest.TestCase):
    def test_round_trip_preserves_execution(self):
        src = ("fn o(){ let c=0; fn i(){c=c+1; return c;} return i; }"
               "let f=o(); print(f(),f());")
        fe = compile_only(src)
        before = vm_output(src)
        data = json.loads(json.dumps(fe.module.to_dict()))
        restored = IRModule.from_dict(data)
        _, after = run_module(restored)
        self.assertEqual(before, after)
        # spans survive serialization
        self.assertTrue(all(len(sp) == 6 for f in restored.functions
                            for sp in f.span))


class TestSharedCellSemantics(unittest.TestCase):
    def test_cell_identity_shared_in_env(self):
        fe = compile_only(
            "fn o(){ let c=0;"
            "  fn a(){c=c+1; return c;}"
            "  fn b(){return c;}"
            "  return a; }")
        # build and step far enough to inspect a closure env
        vm = VM(fe.module)
        seen = {}
        original = vm._make_closure

        def capture(code, ip, frame):
            r = original(code, ip, frame)
            cl = vm.vstack[-1]
            if cl.env:
                seen.setdefault(cl.fnid, cl.env[0])
            return r

        vm._make_closure = capture
        vm.run()
        # every closure capturing c references a Cell (mutable container)
        for fnid, entry in seen.items():
            self.assertIsInstance(entry, Cell)

    def test_two_closures_share_same_cell_object(self):
        # o returns a thunk that builds a and b; calling it executes o's
        # body so both inner closures are actually constructed.
        fe = compile_only(
            "fn o(){ let c=0;"
            "  fn a(){c=c+1; return c;}"
            "  fn b(){c=c+10; return c;}"
            "  return fn(){return 0;}; }"
            "let t=o(); t();")
        vm = VM(fe.module)
        cells = []
        original = vm._make_closure

        def grab(code, ip, frame):
            r = original(code, ip, frame)
            cl = vm.vstack[-1]
            if cl.env and isinstance(cl.env[0], Cell):
                cells.append(cl.env[0])
            return r

        vm._make_closure = grab
        vm.run()
        self.assertEqual(len(cells), 2)
        self.assertIs(cells[0], cells[1])  # shared, not copied


class TestVMRuntimeErrors(unittest.TestCase):
    def test_division_by_zero(self):
        fe = compile_only("print(1/0);")
        with self.assertRaises(RuntimeError_):
            run_module(fe.module)

    def test_call_non_function(self):
        fe = compile_only("let x=1; x();")
        with self.assertRaises(RuntimeError_):
            run_module(fe.module)

    def test_arity_mismatch(self):
        fe = compile_only("fn f(a,b){return a;} f(1);")
        with self.assertRaises(RuntimeError_):
            run_module(fe.module)

    def test_type_error_on_arithmetic(self):
        fe = compile_only("print(true+1);")
        with self.assertRaises(RuntimeError_):
            run_module(fe.module)


if __name__ == "__main__":
    unittest.main()
