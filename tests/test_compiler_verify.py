"""编译器 + 验证器测试：合法程序通过；手工非法字节码被精确拒绝。"""

import struct
import unittest

from slang.bytecode import (
    T_INT, T_VOID,
    OP_ADD, OP_JIF, OP_JUMP, OP_LOAD, OP_NOP, OP_POP, OP_PUSH,
    OP_RET, OP_RETV, OP_STORE, OP_TRUE,
    FuncCode, Module, decode_module,
)
from slang.compiler import compile_source
from slang.verifier import verify_module


def compile_(text: str) -> Module:
    return compile_source(text, filename="<test>")


class TestValidProgramsVerify(unittest.TestCase):
    def assertVerifies(self, text):
        m = compile_(text)
        errs = verify_module(m)
        self.assertEqual(errs, [], "合法程序不应有验证错误: "
                         + "; ".join(e.message for e in errs))
        return m

    def test_empty_main(self):
        self.assertVerifies("fn main() { }")

    def test_arithmetic(self):
        self.assertVerifies("""
        fn add(int a, int b): int { return a + b - 1 * 2 / 3 % 5; }
        fn main() { print add(3, 4); }
        """)

    def test_loop_backedge(self):
        self.assertVerifies("""
        fn sumto(int n): int {
            var i = 1; var acc = 0;
            while (i <= n) { acc = acc + i; i = i + 1; }
            return acc;
        }
        fn main() { print sumto(5); }
        """)

    def test_early_returns(self):
        self.assertVerifies("""
        fn cls(int x): int {
            if (x < 0) { return -1; }
            if (x == 0) { return 0; }
            return 1;
        }
        fn main() { print cls(0); }
        """)

    def test_recursion(self):
        self.assertVerifies("""
        fn fact(int n): int {
            if (n <= 1) { return 1; }
            return n * fact(n - 1);
        }
        fn main() { print fact(4); }
        """)

    def test_bool_merge_definite_assign(self):
        self.assertVerifies("""
        fn f(bool c): int {
            int x;
            if (c) { x = 1; } else { x = 2; }
            return x;
        }
        fn main() { print f(true); }
        """)

    def test_bool_ops(self):
        self.assertVerifies("""
        fn f(int y): bool { return y % 4 == 0 && !(y % 100 == 0) || y % 400 == 0; }
        fn main() { print f(2000); }
        """)


class TestRoundtrip(unittest.TestCase):
    def test_encode_decode_preserves_code(self):
        m = compile_("fn main() { print 1; }")
        raw = m.encode()
        m2 = decode_module(raw)
        self.assertEqual(bytes(m2.funcs[0].code), bytes(m.funcs[0].code))
        self.assertEqual(m2.source, m.source)
        # 再验证一次解码结果
        self.assertEqual(verify_module(m2), [])


def module_with(func: FuncCode) -> Module:
    return Module("bad", "; crafted\n", [func])


def s16(n):
    return struct.pack(">h", n)


class TestHandCraftedInvalidBytecode(unittest.TestCase):
    def _first_error(self, fn):
        errs = verify_module(module_with(fn))
        self.assertTrue(errs, "应当至少报告一条错误")
        return errs[0]

    def test_jump_out_of_bounds(self):
        code = bytes([OP_JUMP]) + s16(100)
        e = self._first_error(FuncCode("f", [], T_VOID, [], bytearray(code)))
        self.assertEqual(e.code, "JUMP_OUT_OF_BOUNDS")

    def test_jump_unaligned_backedge(self):
        # 回边跳到 PUSH 的操作数中间
        code = (bytes([OP_PUSH]) + s16(1) +
                bytes([OP_NOP, OP_JUMP]) + s16(1 - 4))
        e = self._first_error(FuncCode("f", [], T_VOID, [T_INT], bytearray(code)))
        self.assertEqual(e.code, "JUMP_UNALIGNED")

    def test_stack_height_merge(self):
        code = bytearray([OP_TRUE, OP_JIF, 0, 0])   # 0..3 条件
        code += bytes([OP_PUSH]) + s16(8)            # 4..6 假：1 个
        code += bytes([OP_JUMP, 0, 0])               # 7..9
        lt = len(code)                               # 10
        code += bytes([OP_PUSH]) + s16(8)
        code += bytes([OP_PUSH]) + s16(9)            # 真：2 个
        end = len(code)
        code += bytes([OP_RETV])
        struct.pack_into(">h", code, 2, lt - 1)
        struct.pack_into(">h", code, 8, end - 7)
        e = self._first_error(FuncCode("f", [], T_INT, [], bytes(code)))
        self.assertEqual(e.code, "STACK_MERGE_CONFLICT")
        self.assertIn("高度", e.message)

    def test_stack_type_merge(self):
        code = bytearray([OP_TRUE, OP_JIF, 0, 0])
        code += bytes([OP_TRUE, OP_JUMP, 0, 0])
        lt = len(code)
        code += bytes([OP_PUSH]) + s16(5)
        j = len(code)
        code += bytes([OP_JUMP, 0, 0])
        end = len(code)
        code += bytes([OP_RETV])
        struct.pack_into(">h", code, 2, lt - 1)
        struct.pack_into(">h", code, 6, end - 5)
        struct.pack_into(">h", code, j + 1, end - j)
        e = self._first_error(FuncCode("f", [], T_INT, [], bytes(code)))
        self.assertEqual(e.code, "STACK_MERGE_CONFLICT")
        self.assertIn("类型", e.message)

    def test_local_uninitialized(self):
        code = bytes([OP_LOAD, 0, OP_POP, OP_RET])
        e = self._first_error(FuncCode("f", [], T_VOID, [T_INT], bytearray(code)))
        self.assertEqual(e.code, "LOCAL_UNINITIALIZED")

    def test_local_uninitialized_at_merge(self):
        # 真分支写槽0，假分支不写；合流后 LOAD 0
        # TRUE; JIF Lw; JUMP end; Lw: PUSH 7; STORE 0; end: LOAD 0; POP; RET
        code = bytearray([OP_TRUE, OP_JIF, 0, 0])     # 0
        code += bytes([OP_JUMP, 0, 0])                # 4
        lw = len(code)
        code += bytes([OP_PUSH]) + s16(7)
        code += bytes([OP_STORE, 0])
        end = len(code)
        code += bytes([OP_LOAD, 0, OP_POP, OP_RET])
        struct.pack_into(">h", code, 2, lw - 1)
        struct.pack_into(">h", code, 5, end - 4)
        e = self._first_error(FuncCode("f", [], T_VOID, [T_INT], bytes(code)))
        self.assertEqual(e.code, "LOCAL_UNINITIALIZED")

    def test_stack_underflow(self):
        code = bytes([OP_ADD, OP_RET])
        e = self._first_error(FuncCode("f", [], T_VOID, [], bytearray(code)))
        self.assertEqual(e.code, "STACK_UNDERFLOW")

    def test_return_mismatch_void_retv(self):
        code = bytes([OP_PUSH]) + s16(1) + bytes([OP_RETV])
        e = self._first_error(FuncCode("f", [], T_VOID, [], bytearray(code)))
        self.assertEqual(e.code, "RETURN_MISMATCH")

    def test_type_mismatch_add_bool(self):
        # TRUE; PUSH 1; ADD  -> ADD 需要两个 int
        code = bytes([OP_TRUE]) + bytes([OP_PUSH]) + s16(1) + bytes([OP_ADD, OP_POP, OP_RET])
        e = self._first_error(FuncCode("f", [], T_VOID, [], bytearray(code)))
        self.assertEqual(e.code, "TYPE_MISMATCH")

    def test_bad_slot(self):
        code = bytes([OP_LOAD, 9, OP_POP, OP_RET])
        e = self._first_error(FuncCode("f", [], T_VOID, [T_INT], bytearray(code)))
        self.assertEqual(e.code, "BAD_SLOT")

    def test_decode_error_bad_opcode(self):
        code = bytes([0x77])
        e = self._first_error(FuncCode("f", [], T_VOID, [], bytearray(code)))
        self.assertEqual(e.code, "DECODE_ERROR")

    def test_truncated_operand(self):
        code = bytes([OP_PUSH, 0x01])   # 缺第二个立即数字节
        e = self._first_error(FuncCode("f", [], T_VOID, [], bytearray(code)))
        self.assertEqual(e.code, "DECODE_ERROR")

    def test_fall_off_end(self):
        # PUSH 1 后没有返回，直接到末尾
        code = bytes([OP_PUSH]) + s16(1)
        e = self._first_error(FuncCode("f", [], T_INT, [], bytearray(code)))
        self.assertEqual(e.code, "FALL_OFF_END")


class TestShortestPath(unittest.TestCase):
    def test_path_points_to_error(self):
        # 越界跳转前有若干顺序指令，路径应包含它们
        code = (bytes([OP_NOP, OP_NOP, OP_JUMP]) + s16(200))
        m = Module("bad", "", [FuncCode("f", [], T_VOID, [], bytearray(code))])
        errs = verify_module(m)
        self.assertEqual(errs[0].code, "JUMP_OUT_OF_BOUNDS")
        path = errs[0].path
        self.assertEqual(path[0].kind, "entry")
        # 至少经过两个 NOP 的 fallthrough 才到 JUMP
        self.assertGreaterEqual(len(path), 2)
        rendered = errs[0].render_path()
        self.assertIn("JUMP", rendered)

    def test_path_through_backedge(self):
        # 构造带真实回边的循环，循环退出后读未初始化槽。
        # head(0): TRUE; JIF body; JUMP end
        # body(7): NOP; JUMP head          <- 回边
        # end(11): LOAD 0; POP; RET
        code = bytearray([
            OP_TRUE, OP_JIF, 0, 0,     # 0..3  -> body
            OP_JUMP, 0, 0,             # 4..6  -> end
            OP_NOP,                    # 7 body
            OP_JUMP, 0, 0,             # 8..10 -> head（回边）
        ])
        struct.pack_into(">h", code, 2, 7 - 1)
        struct.pack_into(">h", code, 9, 0 - 8)
        end_pc = len(code)             # 11
        code += bytes([OP_LOAD, 0, OP_POP, OP_RET])
        struct.pack_into(">h", code, 5, end_pc - 4)
        m = Module("bad", "", [FuncCode("f", [], T_VOID, [T_INT], bytes(code))])
        errs = verify_module(m)
        # 主要验证：带回边的图能终止分析，并在读未初始化槽处报错
        self.assertTrue(any(e.code == "LOCAL_UNINITIALIZED" for e in errs),
                        [e.code for e in errs])


if __name__ == "__main__":
    unittest.main()
