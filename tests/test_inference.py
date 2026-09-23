"""类型推导核心测试：let 多态、合一、occurs-check、值限制。"""
from __future__ import annotations

import unittest

from tinyinfer.inference import OccursError, UnifyError
from tests.helpers import analyze_err, analyze_ok, infer_type


class IdentityPolymorphismTests(unittest.TestCase):
    """验收一：恒等函数的多次实例化。"""

    SOURCE = (
        "let id = fun x -> x in "
        "let a = id 1 in "
        "let b = id true in "
        "let c = id id 7 in "
        "if b then a + c else 0"
    )

    def test_typechecks_and_evaluates(self) -> None:
        payload = analyze_ok(self.SOURCE)
        self.assertEqual(payload["type"], "int")
        self.assertEqual(payload["value"], "8")

    def test_id_scheme_is_polymorphic(self) -> None:
        payload = analyze_ok("let id = fun x -> x in id 1")
        schemes = {b["name"]: b["scheme"] for b in payload["bindings"]}
        # 顶层 id 的方案必须含 forall
        self.assertIn("forall", schemes["id"])

    def test_two_instantiations_independent(self) -> None:
        # int 实例化与 bool 实例化互不影响
        self.assertEqual(infer_type("let id = fun x -> x in id 1"), "int")
        self.assertEqual(infer_type("let id = fun x -> x in id true"), "bool")
        self.assertEqual(
            infer_type("let id = fun x -> x in (id id) (fun y -> y) 3"),
            "int",
        )

    def test_trace_shows_instantiation_and_generalization(self) -> None:
        payload = analyze_ok(self.SOURCE)
        kinds = [ev["kind"] for ev in payload["trace"]]
        self.assertIn("generalize", kinds)
        self.assertIn("instantiate", kinds)
        instantiations = [
            ev for ev in payload["trace"] if ev["kind"] == "instantiate"
        ]
        # id 至少被实例化 4 次（1/true/id/7 的使用链）
        self.assertGreaterEqual(len(instantiations), 4)


class OccursCheckTests(unittest.TestCase):
    """验收二：递归类型拒绝。"""

    def test_self_application_rejected(self) -> None:
        err = analyze_err("let loop = fun f -> f f in loop loop")
        self.assertEqual(err["kind"], OccursError.__name__)
        # 错误必须指向冲突表达式（应用），且有行列
        self.assertIsNotNone(err["span"])
        self.assertGreaterEqual(err["span"]["start"]["line"], 1)
        self.assertIn("occurs", err["message"])

    def test_occurs_even_naive(self) -> None:
        # occurs-check 与值限制无关：naive 模式同样拒绝
        err = analyze_err(
            "let loop = fun f -> f f in loop loop",
            value_restriction=False,
        )
        self.assertEqual(err["kind"], OccursError.__name__)

    def test_occurs_through_function_argument(self) -> None:
        # 另一处会产生无限类型的结构：对 selfapply 的任何调用
        # （其函数体 f f 在推导阶段就触发 occurs-check）
        err = analyze_err("let selfapply = fun f -> f f in selfapply (fun x -> x)")
        self.assertEqual(err["kind"], OccursError.__name__)

    def test_non_recursive_polymorphism_ok(self) -> None:
        # 递归数据类型需要真正的类型构造器；本语言没有，确保普通
        # 多态函数不被误伤
        self.assertEqual(
            infer_type("let k = fun x -> fun y -> x in k 1 true"), "int"
        )


class ReferenceUnsoundnessTests(unittest.TestCase):
    """验收三：引用导致的不健全反例与值限制。"""

    SOURCE = "let r = new_ref unit in r <- true; deref r + 1"

    def test_value_restriction_rejects(self) -> None:
        # 默认开启值限制：应用不是语法值，r 保持单态，第二次以 int
        # 使用时与第一次的 bool 实例化产生冲突
        err = analyze_err(self.SOURCE)
        self.assertEqual(err["kind"], UnifyError.__name__)
        self.assertIsNotNone(err["span"])
        message = err["message"]
        self.assertTrue("int" in message and "bool" in message)

    def test_naive_accepts_but_crashes_at_runtime(self) -> None:
        payload = analyze_ok(self.SOURCE, value_restriction=False)
        # naive 多态：类型检查通过，最终类型看起来很合理
        self.assertEqual(payload["type"], "int")
        # 但求值必然在 true + 1 处崩溃 —— 类型系统的不健全被实证
        self.assertIsNotNone(payload["eval_error"])
        self.assertEqual(payload["eval_error"]["kind"], "RuntimeFailure")
        self.assertIn("非 int", payload["eval_error"]["message"])

    def test_sound_ref_use_ok(self) -> None:
        payload = analyze_ok(
            "let r = ref 1 in r <- deref r + 1; deref r"
        )
        self.assertEqual(payload["type"], "int")
        self.assertEqual(payload["value"], "2")

    def test_new_ref_sound_when_monomorphic(self) -> None:
        # 值限制下 r 是单态；先后按同一类型使用完全合法
        payload = analyze_ok(
            "let r = new_ref unit in r <- 40; r <- deref r + 2; deref r"
        )
        self.assertEqual(payload["type"], "int")
        self.assertEqual(payload["value"], "42")

    def test_ref_cell_is_monomorphic_after_generalization_block(self) -> None:
        # 值限制阻止多态使用：同一单元被两种不兼容类型使用即冲突
        err = analyze_err(
            "let r = new_ref unit in "
            "r <- 0; "
            "r <- true"
        )
        self.assertEqual(err["kind"], UnifyError.__name__)


class GeneralTypeInferenceTests(unittest.TestCase):
    def test_arithmetic_and_comparison(self) -> None:
        self.assertEqual(infer_type("1 + 2 * 3"), "int")
        self.assertEqual(infer_type("1 < 2"), "bool")
        self.assertEqual(infer_type("if 1 == 1 then 1 else 2"), "int")

    def test_if_branch_mismatch_location(self) -> None:
        err = analyze_err("if true then 1 else false")
        self.assertEqual(err["kind"], UnifyError.__name__)
        # 指向 else 分支
        self.assertEqual(err["span"]["start"]["line"], 1)

    def test_if_condition_must_be_bool(self) -> None:
        err = analyze_err("if 1 then 1 else 2")
        self.assertEqual(err["kind"], UnifyError.__name__)

    def test_unbound_variable(self) -> None:
        err = analyze_err("x + 1")
        self.assertEqual(err["kind"], "UnboundError")

    def test_arity_mismatch(self) -> None:
        err = analyze_err("let f = fun x -> x + 1 in f 1 2")
        self.assertEqual(err["kind"], UnifyError.__name__)

    def test_recursive_factorial(self) -> None:
        src = (
            "let rec fact n = if n <= 1 then 1 else n * fact (n - 1) "
            "in fact 5"
        )
        payload = analyze_ok(src)
        self.assertEqual(payload["type"], "int")
        self.assertEqual(payload["value"], "120")

    def test_recursive_function_generalized_when_value(self) -> None:
        # 递归函数是语法值，可以一般化后多次实例化
        src = (
            "let rec id2 x = x in "
            "let a = id2 1 in id2 true"
        )
        self.assertEqual(infer_type(src), "bool")

    def test_param_annotation_checked(self) -> None:
        err = analyze_err("let f (x: int) = x in f true")
        self.assertEqual(err["kind"], UnifyError.__name__)

    def test_result_annotation_checked(self) -> None:
        payload = analyze_ok("let f (x: int): int = x + 1 in f 2")
        self.assertEqual(payload["value"], "3")
        err = analyze_err("let f (x: int): bool = x in f 1")
        self.assertEqual(err["kind"], UnifyError.__name__)

    def test_wrong_annotation_rejected(self) -> None:
        err = analyze_err("let f (x: int): bool = x in f 1")
        self.assertEqual(err["kind"], UnifyError.__name__)

    def test_ref_annotation_and_deref(self) -> None:
        src = "let r: int ref = ref 0 in r <- 5; deref r"
        payload = analyze_ok(src)
        self.assertEqual(payload["value"], "5")

    def test_unit_and_sequence(self) -> None:
        payload = analyze_ok("let r = ref 0 in r <- 1; unit")
        self.assertEqual(payload["type"], "unit")

    def test_higher_order_polymorphism_multiple_types(self) -> None:
        # twice 在同一程序中分别以 int 和 bool 实例化
        src = (
            "let twice f x = f (f x) in "
            "let n = twice (fun y -> y + 1) 10 in "
            "let b = twice not true in "
            "if b then n else 0"
        )
        payload = analyze_ok(src)
        self.assertEqual(payload["type"], "int")
        self.assertEqual(payload["value"], "12")

    def test_conflict_span_points_at_subexpression(self) -> None:
        # 第 3 行的错误实参必须被精确定位
        src = "let f (x: int) = x\nin\nf true\n"
        err = analyze_err(src)
        self.assertEqual(err["span"]["start"]["line"], 3)
        self.assertEqual(err["span"]["start"]["column"], 3)


if __name__ == "__main__":
    unittest.main()
