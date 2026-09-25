"""Core type-inference tests.

Covers the acceptance criteria explicitly:
* identity function instantiated multiple times at different types;
* recursive types rejected by the occurs check;
* the reference unsoundness counterexample under the value restriction;
* plus unification, let-polymorphism, recursion and located-error checks.
"""

import unittest

from miniml.infer import InferError, infer_program
from miniml.parser import parse
from miniml.types import (
    Scheme,
    TVar,
    free_vars,
    mk_arrow,
    t_bool,
    t_int,
    type_str,
    unify,
    UnifyError,
)


def infer(src: str, value_restriction: bool = True, trace: bool = False):
    return infer_program(parse(src), value_restriction=value_restriction, trace=trace)


def bind_schemes(result):
    return {b.name: b.scheme for b in result.bindings}


# --------------------------------------------------------------- unification


class TestUnification(unittest.TestCase):
    def test_var_binds_to_type(self):
        a = TVar()
        unify(a, t_int)
        self.assertIs(a.prune(), t_int)

    def test_identical_vars(self):
        a = TVar()
        unify(a, a)  # must not raise

    def test_constructor_clash(self):
        with self.assertRaises(UnifyError) as cm:
            unify(t_int, t_bool)
        self.assertFalse(cm.exception.cycle)

    def test_arrow_decomposition(self):
        a, b = TVar(), TVar()
        unify(mk_arrow(a, t_int), mk_arrow(t_bool, b))
        self.assertIs(a.prune(), t_bool)
        self.assertIs(b.prune(), t_int)

    def test_occurs_check_direct(self):
        a = TVar()
        with self.assertRaises(UnifyError) as cm:
            unify(a, mk_arrow(a, t_int))
        self.assertTrue(cm.exception.cycle)

    def test_occurs_check_through_links(self):
        a, b = TVar(), TVar()
        unify(a, b)
        with self.assertRaises(UnifyError) as cm:
            unify(b, mk_arrow(a, t_int))
        self.assertTrue(cm.exception.cycle)

    def test_free_vars_respects_links(self):
        a, b = TVar(), TVar()
        unify(a, b)
        vs = free_vars([a])
        self.assertEqual(len(vs), 1)


# ------------------------------------------------------------ let-polymorphism


class TestLetPolymorphism(unittest.TestCase):
    def test_identity_has_polymorphic_scheme(self):
        r = infer("let id = fun x -> x ;;")
        sch = bind_schemes(r)["id"]
        self.assertEqual(len(sch.qvars), 1)
        self.assertEqual(type_str(sch.body), "a -> a")

    def test_identity_instantiated_at_three_types(self):
        src = """
        let id = fun x -> x ;;
        let as_int  = id 1 ;;
        let as_bool = id true ;;
        let as_fun  = id id ;;
        """
        r = infer(src)
        sch = bind_schemes(r)
        self.assertEqual(type_str(sch["as_int"].body), "int")
        self.assertEqual(type_str(sch["as_bool"].body), "bool")
        # id id : identity instantiated at an arrow type, not unified with int
        self.assertIn("->", type_str(sch["as_fun"].body))

    def test_instances_do_not_constrain_each_other(self):
        # fst = fun x -> fun y -> x ; using at int,int then bool,bool
        src = """
        let fst = fun x -> fun y -> x ;;
        let a = fst 1 2 ;;
        let b = fst true false ;;
        """
        r = infer(src)
        sch = bind_schemes(r)
        self.assertEqual(type_str(sch["a"].body), "int")
        self.assertEqual(type_str(sch["b"].body), "bool")

    def test_monomorphic_lambda_parameter(self):
        # fun x -> (x + 1, x && true) must fail: x cannot be int and bool
        src = "let bad = fun x -> (if x + 1 = 2 then x && true else false) ;;"
        with self.assertRaises(InferError):
            infer(src)

    def test_polymorphic_recursive_function(self):
        src = """
        let rec apply_twice = fun f -> fun x -> f (f x) ;;
        let u = apply_twice (fun n -> n + 1) 10 ;;
        let v = apply_twice (fun b -> b) true ;;
        """
        r = infer(src)
        sch = bind_schemes(r)
        self.assertEqual(len(sch["apply_twice"].qvars), 1)
        self.assertEqual(type_str(sch["u"].body), "int")
        self.assertEqual(type_str(sch["v"].body), "bool")

    def test_factorial(self):
        src = "let rec fact = fun n -> if n <= 1 then 1 else n * fact (n - 1) ;;"
        r = infer(src)
        self.assertEqual(type_str(bind_schemes(r)["fact"].body), "int -> int")


# ----------------------------------------------------------- occurs check


class TestOccursCheck(unittest.TestCase):
    def test_self_application_rejected(self):
        with self.assertRaises(InferError) as cm:
            infer("let loop = fun x -> x x ;;")
        self.assertTrue(cm.exception.cycle)

    def test_occurs_error_span_is_on_the_application(self):
        with self.assertRaises(InferError) as cm:
            infer("let loop = fun x -> x x ;;")
        # the inner argument `x` on line 1 col 23
        self.assertEqual(cm.exception.span.start.col, 23)

    def test_more_infinite_type(self):
        # (fun f -> f f) (fun g -> g g) also fails the occurs check
        with self.assertRaises(InferError) as cm:
            infer("fun f -> f f")
        self.assertTrue(cm.exception.cycle)


# -------------------------------------------------------- value restriction


class TestValueRestriction(unittest.TestCase):
    SOUND_COUNTEREXAMPLE = """
    let r = ref (fun x -> x) in
    r := (fun n -> n + 1);
    let f = !r in
    let a = f true in
    let b = f 0 in
    b
    """

    def test_counterexample_rejected_with_value_restriction(self):
        with self.assertRaises(InferError) as cm:
            infer(self.SOUND_COUNTEREXAMPLE, value_restriction=True)
        # location points at the conflicting application `f true` (line 5)
        self.assertEqual(cm.exception.span.start.line, 5)
        self.assertFalse(cm.exception.cycle)

    def test_counterexample_accepted_without_restriction(self):
        # Turning VR off must make naive inference accept it — demonstrating
        # exactly which rule is responsible for soundness.
        r = infer(self.SOUND_COUNTEREXAMPLE, value_restriction=False)
        self.assertEqual(type_str(r.expr_type), "int")

    def test_nonvalue_binding_is_monomorphic(self):
        # `let f = !r` where r : ref(a->a) is not a syntactic value:
        # f must not be generalized.
        src = """
        let r = ref (fun x -> x) in
        r := (fun n -> n + 1);
        let f = !r in
        f 0
        """
        r = infer(src)  # accepted: int usage only
        self.assertEqual(type_str(r.expr_type), "int")

    def test_value_binding_is_generalized(self):
        # a lambda wrapped syntactically remains polymorphic
        src = "let k = fun a -> fun b -> a ;;\nlet x = k 1 true ;;\nlet y = k false 1 ;;"
        r = infer(src)
        sch = bind_schemes(r)
        self.assertEqual(len(sch["k"].qvars), 2)
        self.assertEqual(type_str(sch["x"].body), "int")
        self.assertEqual(type_str(sch["y"].body), "bool")

    def test_ref_allocates_monotypically(self):
        # let r = ref id ; r := succ ; !r true  -- rejected
        src = """
        let r = ref (fun x -> x) in
        r := (fun n -> n + 1);
        (!r) true
        """
        with self.assertRaises(InferError):
            infer(src)

    def test_plain_ref_operations(self):
        src = """
        let r = ref 0 in
        r := !r + 1;
        !r
        """
        r = infer(src)
        self.assertEqual(type_str(r.expr_type), "int")


# --------------------------------------------------------- located errors


class TestErrorLocations(unittest.TestCase):
    def test_unbound_variable(self):
        with self.assertRaises(InferError) as cm:
            infer("zzz")
        self.assertEqual(cm.exception.span.start.col, 1)

    def test_int_bool_clash_location(self):
        with self.assertRaises(InferError) as cm:
            infer("1 + true")
        # caret on `true` (line 1 col 5)
        self.assertEqual(cm.exception.span.start.col, 5)

    def test_argument_vs_function_error_related_span(self):
        with self.assertRaises(InferError) as cm:
            infer("(fun n -> n + 1) true")
        self.assertEqual(cm.exception.span.start.col, 18)  # argument `true`
        self.assertTrue(cm.exception.related)

    def test_if_branch_mismatch(self):
        with self.assertRaises(InferError) as cm:
            infer("if true then 1 else false")
        self.assertIn("same type", cm.exception.message)

    def test_multiline_error_line(self):
        src = "let x = 1 in\nlet y = true in\nx + y"
        with self.assertRaises(InferError) as cm:
            infer(src)
        self.assertEqual(cm.exception.span.start.line, 3)


# --------------------------------------------------------------- trace


class TestTrace(unittest.TestCase):
    def test_trace_records_generalize_and_instantiate(self):
        r = infer("let id = fun x -> x ;;\nlet z = id 1 ;;", trace=True)
        steps = {e.step for e in r.trace}
        self.assertIn("generalize", steps)
        self.assertIn("instantiate", steps)
        self.assertIn("unify", steps)

    def test_trace_can_be_disabled(self):
        r = infer("let id = fun x -> x ;;", trace=False)
        self.assertEqual(r.trace, [])


if __name__ == "__main__":
    unittest.main()
