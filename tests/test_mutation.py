"""单字节变异测试（验收核心）。

对每个合法示例施加全部 targeted 单字节变异，要求：
1. 每个变异都被某一层安全处置（解码 / 验证 / 运行期错误 / 燃料内跑完）；
2. **绝不出现 invariant_broken / verifier_crashed / interpreter_crashed**
   —— 即“通过验证的字节码不会在解释器里栈下溢（或触发其它结构不变量破坏）”。

并针对三个明确的覆盖目标断言错误码出现：
- 回边：在循环程序上变异，能产生 JUMP_UNALIGNED / 合流相关错误；
- 异常返回：在 early-return 程序上变异，能产生 RETURN_MISMATCH；
- 越界跳转：在任意程序上变异，能产生 JUMP_OUT_OF_BOUNDS。
"""

import os
import unittest

from slang.campaign import run_campaign
from slang.compiler import compile_source
from slang.mutator import apply_mutation, targeted_mutations
from slang.bytecode import decode_module
from slang.verifier import verify_module

EX = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "examples")

FORBIDDEN = {"invariant_broken", "verifier_crashed", "interpreter_crashed"}


def load_example(name):
    with open(os.path.join(EX, name), "r", encoding="utf-8") as f:
        return compile_source(f.read(), filename=name)


class TestMutationSafety(unittest.TestCase):
    def _campaign_safe(self, module, fuel=5_000):
        rep = run_campaign(module, strategy="targeted", fuel=fuel)
        cats = rep["by_category"]
        bad = {c: n for c, n in cats.items() if c in FORBIDDEN}
        self.assertFalse(
            bad,
            f"出现不变量破坏/崩溃: {bad}；示例: "
            + str({k: v["message"] for k, v in rep["examples"].items()
                   if k in FORBIDDEN}))
        return rep

    def test_sum_loop_no_invariant_break(self):
        rep = self._campaign_safe(load_example("sum_loop.sl"))
        self.assertGreater(rep["total"], 100)

    def test_early_return_no_invariant_break(self):
        self._campaign_safe(load_example("early_return.sl"))

    def test_bool_logic_no_invariant_break(self):
        self._campaign_safe(load_example("bool_logic.sl"))

    def test_uninit_read_no_invariant_break(self):
        self._campaign_safe(load_example("uninit_read.sl"))


class TestMutationCoverage(unittest.TestCase):
    def test_oob_jump_is_producible(self):
        # 把某个 JUMP/JIF 的操作数字节改成极值，必产生越界或对齐错误
        m = load_example("sum_loop.sl")
        found = False
        for mut in targeted_mutations(m):
            raw = apply_mutation(m, mut)
            mm = decode_module(raw)
            errs = verify_module(mm)
            if errs and errs[0].code in ("JUMP_OUT_OF_BOUNDS", "JUMP_UNALIGNED"):
                found = True
                break
        self.assertTrue(found, "应能通过单字节变异造出越界/未对齐跳转")

    def test_backedge_categories_on_loop(self):
        rep = run_campaign(load_example("sum_loop.sl"), fuel=3_000)
        codes = rep["verify_error_codes"]
        # 回边/跳转相关错误至少出现一类
        self.assertTrue(
            codes.get("JUMP_OUT_OF_BOUNDS", 0)
            + codes.get("JUMP_UNALIGNED", 0)
            + codes.get("STACK_MERGE_CONFLICT", 0) > 0)

    def test_return_mismatch_on_early_return_program(self):
        rep = run_campaign(load_example("early_return.sl"), fuel=3_000)
        self.assertIn("RETURN_MISMATCH", rep["verify_error_codes"])

    def test_uninitialized_on_uninit_program(self):
        rep = run_campaign(load_example("uninit_read.sl"), fuel=3_000)
        # 操作码/槽号变异应能让读取落到未初始化或坏槽
        codes = rep["verify_error_codes"]
        self.assertTrue(
            codes.get("LOCAL_UNINITIALIZED", 0)
            + codes.get("BAD_SLOT", 0) > 0)


class TestExhaustiveSmallFunction(unittest.TestCase):
    def test_exhaustive_tiny_function_all_safe(self):
        # 对一个极小函数做“每字节 255 种取值”的全量变异，
        # 仍然不允许任何不变量破坏。
        m = compile_source("fn main() { print 1; }")
        rep = run_campaign(m, strategy="exhaustive", fuel=2_000)
        self.assertGreater(rep["total"], 50)
        for forbidden in FORBIDDEN:
            self.assertEqual(rep["by_category"].get(forbidden, 0), 0)

    def test_single_byte_mutation_contract(self):
        # 变异后的模块与原模块编码长度一致，且恰有一个 code 字节不同
        m = load_example("sum_loop.sl")
        raw0 = m.encode()
        muts = targeted_mutations(m)
        mut = muts[0]
        raw1 = apply_mutation(m, mut)
        self.assertEqual(len(raw0), len(raw1))
        diffs = [i for i, (a, b) in enumerate(zip(raw0, raw1)) if a != b]
        self.assertEqual(len(diffs), 1)
        self.assertEqual(raw1[diffs[0]], mut.value)


if __name__ == "__main__":
    unittest.main()
