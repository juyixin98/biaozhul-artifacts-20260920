"""Tests for the small evaluator: accepted programs compute correct values."""

import unittest

from miniml.eval import TypePanic, eval_program
from miniml.infer import infer_program
from miniml.pipeline import compile_source, evaluate, CompileFailure
from miniml.parser import parse


def run(src: str, value_restriction: bool = True):
    program, _ = compile_source(src, value_restriction=value_restriction)
    return evaluate(program)


class TestEvaluator(unittest.TestCase):
    def test_arith(self):
        r = run("(1 + 2) * 3 - 4 / 2")
        self.assertEqual(r.value, 7)

    def test_division_truncates_to_zero(self):
        self.assertEqual(run("-7 / 2").value, -3)

    def test_division_by_zero_is_panic(self):
        with self.assertRaises(CompileFailure) as cm:
            run("1 / 0")
        self.assertEqual(cm.exception.code, "E005")

    def test_bool_and_comparison(self):
        self.assertEqual(run("1 < 2 && 3 <> 4").value.v, True)
        self.assertEqual(run("1 = 1").value.v, True)

    def test_if(self):
        self.assertEqual(run("if false then 1 else 2").value, 2)

    def test_let_and_lambda(self):
        src = "let add = fun a -> fun b -> a + b in add 40 2"
        self.assertEqual(run(src).value, 42)

    def test_recursion_factorial(self):
        src = "let rec fact = fun n -> if n <= 1 then 1 else n * fact (n-1) in fact 6"
        self.assertEqual(run(src).value, 720)

    def test_references(self):
        src = """
        let sum = ref 0 in
        sum := !sum + 10;
        sum := !sum + 5;
        !sum
        """
        self.assertEqual(run(src).value, 15)

    def test_identity_at_runtime(self):
        src = "let id = fun x -> x in id 7"
        self.assertEqual(run(src).value, 7)

    def test_unsound_program_panics_at_runtime_without_vr(self):
        # The exact counterexample: accepted when VR is off, crashes
        # dynamically because one shared cell is viewed at two types.
        src = """
        let r = ref (fun x -> x) in
        r := (fun n -> n + 1);
        let f = !r in
        let a = f true in
        f 0
        """
        with self.assertRaises(CompileFailure) as cm:
            run(src, value_restriction=False)
        self.assertEqual(cm.exception.phase, "eval")
        self.assertEqual(cm.exception.code, "E005")


class TestEvaluatorDirect(unittest.TestCase):
    """Run expressions without inference to test the panic machinery itself."""

    def test_untyped_apply_nonfunction_panics(self):
        p = parse("1 2")
        with self.assertRaises(TypePanic):
            eval_program(p)


if __name__ == "__main__":
    unittest.main()
