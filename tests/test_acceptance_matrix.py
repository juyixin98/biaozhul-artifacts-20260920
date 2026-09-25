"""Additional end-to-end property-style cases (accept/reject matrix)."""

import unittest

from miniml.pipeline import CompileFailure, compile_source, evaluate


def is_accepted(src: str, value_restriction: bool = True) -> bool:
    try:
        compile_source(src, value_restriction=value_restriction, trace=False)
        return True
    except CompileFailure:
        return False


ACCEPT = [
    "(* a (* b *) c *)\nlet x = 1 + 2 ;;",
    "let compose = fun f -> fun g -> fun x -> f (g x) in "
    "compose (fun n -> n * 2) (fun n -> n + 1) 5",
    "let k = fun a -> fun b -> a in k 1",
    "fun x -> fun y -> fun z -> (x z) (y z)",
    "if true then unit",
    "let rec loop = fun f -> fun x -> loop f (f x) in loop (fun n -> n + 1) 0",
    "1 ; true ; 42",
    "let x = 1 in let x = true in if x then 1 else 2",
    "let r = ref (fun x -> x) in r := (fun n -> n + 1); (!r) 1",
    "-5 + 2",
    # generic recursion used at two unrelated types
    "let rec apply = fun f -> fun x -> f x in "
    "let a = apply (fun n -> n + 1) 0 in "
    "let b = apply (fun b -> b) true in b",
]

REJECT = [
    "let r = ref 0 in r := true",
    "!1",
    "if 1 then 2 else 3",
    "true + 1",
    "foo 1",
    "if true then 1",
    "1 = 2 = 3",
    "-true",
    "1 := 2",
    "ref 1 + ref 2",
    "fun x -> x x",
    "(fun f -> f f) (fun g -> g)",
]


class TestAcceptReject(unittest.TestCase):
    def test_accepted_programs(self):
        for src in ACCEPT:
            with self.subTest(src=src):
                self.assertTrue(is_accepted(src), msg=src)

    def test_rejected_programs(self):
        for src in REJECT:
            with self.subTest(src=src):
                self.assertFalse(is_accepted(src), msg=src)

    def test_accepted_programs_evaluate_when_appropriate(self):
        # values for a few executable accepted programs
        cases = {
            "1 ; true ; 42": 42,
            "-5 + 2": -3,
            "let compose = fun f -> fun g -> fun x -> f (g x) in "
            "compose (fun n -> n * 2) (fun n -> n + 1) 5": 12,
        }
        for src, expected in cases.items():
            with self.subTest(src=src):
                p, _ = compile_source(src, trace=False)
                r = evaluate(p)
                self.assertEqual(r.value, expected)


if __name__ == "__main__":
    unittest.main()
