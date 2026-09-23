"""单字节变异活动 + 验证器健全性（soundness）测试。

核心不变式：对合法模块做任意单字节（单 bit）变异后，完整流水线
解码 -> 验证 -> 解释执行 中：

* 要么在解码/验证阶段被拒绝；
* 要么验证通过并正常结束（或触发资源限制/除零等*带检查的*运行期错误）；

**绝不允许解释器因操作数栈下溢/未初始化读取而崩溃**。
"""

import io
import unittest

from byteverifier import mutate as mut
from byteverifier.bytecode import (
    JUMP_OPS, OPERAND_SIZE, decode_module, OP_JMP, OP_JIF, OP_RET,
)
from byteverifier.common import ToolError
from byteverifier.compiler import compile_source
from byteverifier.interpreter import Interpreter
from byteverifier.verifier import verify_module

# 小模块：穷举单 bit 翻转成本可控（~ 模块字节数 * 8）
SMALL_PROGRAM = """
int sumto(int n) {
    int s = 0;
    int i = 1;
    while (i <= n) { s = s + i; i = i + 1; }
    return s;
}
void main() {
    print_int(sumto(4));
    bool c = true;
    if (c) { print_int(1); } else { print_int(2); }
}
"""

# 允许在“变异后仍通过验证”时运行阶段出现的错误种类（均为显式检查，非栈下溢）
SAFE_RUNTIME_KINDS = {"fuel.exhausted", "depth.exceeded", "div.zero"}
# 绝对不允许的解释器内部错误（意味着验证器漏检）。
# 注意：verify:stack.underflow 是验证器的*正确拒绝*，不算不健全；
# 只有解释器阶段（runtime:）的内部错误才是 soundness 漏洞。
UNSOUND_MARKERS = ("runtime:pc.invalid", "runtime:stack", "IndexError",
                   "runtime:local.uninit.internal", "runtime:opcode.internal",
                   "runtime:call.oob.internal")


def classify(mutated: bytes):
    """返回阶段分类字符串: decode:<kind> / verify:<kind> / ran / runtime:<kind>。"""
    try:
        module = decode_module(mutated)
    except ToolError as e:
        return f"decode:{e.kind}"
    try:
        verify_module(module)
    except ToolError as e:
        return f"verify:{e.kind}"
    # 验证通过：尝试运行（低 fuel，防死循环）
    if module.by_name("main") is None:
        return "ran:no-main"
    try:
        interp = Interpreter(module, fuel=5_000, out=io.StringIO())
        interp.call_main()
        return "ran:ok"
    except ToolError as e:
        return f"runtime:{e.kind}"


class TestMutationCampaign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.module = compile_source(SMALL_PROGRAM)
        cls.blob = cls.module.encode()
        cls.results = {}
        for i, bit, mutated in mut.all_single_bit_flips(cls.blob):
            cls.results[(i, bit)] = classify(mutated)

    def test_no_unsound_outcome(self):
        bad = {k: v for k, v in self.results.items()
               if any(m in v for m in UNSOUND_MARKERS)}
        self.assertEqual(bad, {})

    def test_coverage_decode_errors(self):
        kinds = {v.split(":", 1)[1] for v in self.results.values()
                 if v.startswith("decode:")}
        # 头部/常量/操作码被翻转，解码期至少应拦住若干类
        self.assertIn("truncated", kinds)

    def test_coverage_verify_errors(self):
        kinds = {v.split(":", 1)[1] for v in self.results.values()
                 if v.startswith("verify:")}
        # 穷举翻转代码段必然产生验证期错误；
        # jump.oob 是否出现取决于跳转立即数是否恰好被翻到高位
        self.assertTrue(kinds, "没有变异体触发验证错误，活动设计有问题")

    def test_coverage_oob_jump_targeted(self):
        # 定向变异保证：把每个跳转目标的低字节都换成 0xFF，至少一个越界
        code_start, code_end = mut.code_region(self.blob)
        main = self.module.by_name("main")
        seen_oob = False
        for f in self.module.functions:
            for ins in f.instructions:
                if ins.opcode in JUMP_OPS:
                    # 该指令在哪个函数？演示只用 main 的 code_region，
                    # 直接对全局 blob 用 main 区域 + 手工定位：
                    pass
        # 针对 main 的所有跳转：用其自身编码偏移
        blob = self.blob
        start, end = mut.code_region(blob)
        for ins in main.instructions:
            if ins.opcode in JUMP_OPS:
                hi = start + ins.pc + 2
                mutant = blob[:hi] + bytes([0xFF]) + blob[hi + 1:]
                r = classify(mutant)
                if r == "verify:jump.oob":
                    seen_oob = True
        self.assertTrue(seen_oob, "定向越界跳转变异没有触发 jump.oob")

    def test_coverage_back_edge(self):
        # while 回边（目标 pc <= 自身 pc）必须在正常代码中存在
        back = [ins for f in self.module.functions for ins in f.instructions
                if ins.opcode in JUMP_OPS and ins.operand <= ins.pc]
        self.assertTrue(back, "示例程序应包含回边")

    def test_coverage_abnormal_return_mutation(self):
        # RETV <-> RET 单字节互换（0x51 <-> 0x52 差 1 bit）
        start, _ = mut.code_region(self.blob)
        retv_ins = next(
            ins for f in self.module.functions for ins in f.instructions
            if ins.opcode == 0x51
        )
        idx = start + retv_ins.pc
        mutant = mut.flip_bit(self.blob, idx, 0)   # 0x51 ^1 = 0x50 (CALL)
        r = classify(mutant)
        self.assertTrue(
            r.startswith(("decode:", "verify:", "ran:")),
            f"RETV->CALL 变异结果异常: {r}",
        )

    def test_coverage_abnormal_return_replace(self):
        # 真正的“异常返回”：把 int 函数唯一的 RETV(0x51) 单字节替换成 RET(0x52)。
        # 模块含 sumto(有 RETV) + main(void)，直接在整个 blob 里找 sumto 的
        # 函数区域：重新编译只含一个 int 函数的模块更稳。
        src = """
        int f(int n) {
            if (n < 0) { return 0; }
            return n + 1;
        }
        void main() { print_int(f(3)); }
        """
        mod = compile_source(src)
        f_code = mod.by_name("f")
        blob = mod.encode()
        cstart, cend = mut.code_region(blob)   # 第一个函数即 f
        retv = next(ins for ins in f_code.instructions if ins.opcode == 0x51)
        mutant = mut.replace_byte(blob, cstart + retv.pc, 0x52)
        r = classify(mutant)
        self.assertTrue(
            r.startswith("verify:"),
            f"RETV->RET 应被验证器拒绝，实际 {r}",
        )

    def test_random_byte_replacements_stay_safe(self):
        # 再做一批“整字节替换”，同样不得出现不健全结果
        blob = self.blob
        for i in range(0, len(blob), 7):
            for val in (0x00, 0xFF, 0x40, 0x51):
                mutant = mut.replace_byte(blob, i, val)
                r = classify(mutant)
                self.assertFalse(
                    any(m in r for m in UNSOUND_MARKERS),
                    f"byte={i} val={val} -> {r}",
                )


class TestHandBuiltEdgeCases(unittest.TestCase):
    def test_jump_to_end_nonvoid_rejected(self):
        # 非 void 函数直接 JMP 到末尾：异常返回
        from byteverifier import bytecode as bc2
        from byteverifier.bytecode import Instruction
        ins = [Instruction(OP_JMP, 3, pc=0)]
        code = bc2.CodeObject(
            name="f", ret_tag=bc2.TAG_INT, param_tags=[], local_tags=[],
            instructions=ins, consts=[], max_stack=0,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(bc2.Module([code]))
        self.assertEqual(cm.exception.kind, "return.missing")

    def test_verified_program_cannot_underflow_defense(self):
        # 直接验证一批合法程序并运行，确认没有任何“验证通过但运行炸栈”
        programs = [SMALL_PROGRAM]
        for src in programs:
            module = compile_source(src)
            verify_module(module)
            interp = Interpreter(module, fuel=20_000, out=io.StringIO())
            interp.call_main()  # 不抛异常即可


if __name__ == "__main__":
    unittest.main()
