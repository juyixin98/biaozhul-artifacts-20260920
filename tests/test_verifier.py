"""验证器必须拒绝的非法程序 / 非法字节码。"""

import unittest

from byteverifier import bytecode as bc
from byteverifier.bytecode import (
    JUMP_OPS, TAG_BOOL, TAG_INT, TAG_VOID, CodeObject, Instruction, Module,
    OP_JMP, OP_LOAD_CONST, OP_POP, OP_RETV, OP_RET, OP_ADD,
)
from byteverifier.common import ToolError
from byteverifier.compiler import compile_source
from byteverifier.verifier import verify_module


def expect_kind(src: str) -> str:
    try:
        module = compile_source(src)
        verify_module(module)
    except ToolError as e:
        return e.kind
    raise AssertionError("本应验证失败，但通过了")


class TestInvalidSource(unittest.TestCase):
    def test_uninit_on_then_branch(self):
        src = """
        void main() {
            int x;
            if (true) { x = 1; }
            print_int(x);
        }
        """
        self.assertEqual(expect_kind(src), "local.uninit")

    def test_uninit_while_head(self):
        # while 头处合流：首次进入时 x 未初始化
        src = """
        void main() {
            int i = 0;
            while (i < 3) {
                int x;
                x = i;
                print_int(x);
                i = i + 1;
            }
        }
        """
        # x 在 while 体内声明：每条使用前都赋值，合法
        module = compile_source(src)
        verify_module(module)

    def test_use_before_assign_straightline(self):
        src = """
        void main() {
            int x;
            print_int(x);
        }
        """
        self.assertEqual(expect_kind(src), "local.uninit")

    def test_missing_return(self):
        src = """
        int f(bool c) { if (c) { return 1; } }
        void main() { print_int(f(false)); }
        """
        self.assertEqual(expect_kind(src), "return.missing")

    def test_value_return_in_void(self):
        src = "void f() { return 3; } void main() { f(); }"
        self.assertEqual(expect_kind(src), "type.operand")  # RETV 弹出的不是 void

    def test_return_without_value_in_int(self):
        src = "int f() { return; } void main() { }"
        self.assertEqual(expect_kind(src), "return.value")

    def test_int_plus_bool(self):
        src = """
        void main() {
            bool b = true;
            print_int(1 + b);
        }
        """
        self.assertEqual(expect_kind(src), "type.operand")

    def test_not_on_int(self):
        src = "void main() { bool b = !1; print_bool(b); }"
        self.assertEqual(expect_kind(src), "type.operand")

    def test_condition_must_be_bool(self):
        src = "void main() { if (3) { } }"
        self.assertEqual(expect_kind(src), "type.operand")

    def test_assign_wrong_type(self):
        src = """
        void main() {
            int x = 1;
            x = true;
            print_int(x);
        }
        """
        self.assertEqual(expect_kind(src), "type.local")

    def test_arg_type_mismatch(self):
        src = """
        int f(int n) { return n; }
        void main() { print_int(f(true)); }
        """
        self.assertEqual(expect_kind(src), "type.arg")

    def test_arg_count_compiler(self):
        src = "int f(int n) { return n; } void main() { print_int(f()); }"
        with self.assertRaises(ToolError) as cm:
            compile_source(src)
        self.assertEqual(cm.exception.kind, "arg.count")

    def test_unknown_var_compiler(self):
        with self.assertRaises(ToolError) as cm:
            compile_source("void main() { x = 1; }")
        self.assertEqual(cm.exception.kind, "unknown.variable")


# ---------- 手工构造字节码：覆盖“源码层造不出来”的错误 ----------

def _mod(main_code: CodeObject) -> Module:
    return Module([main_code])


class TestMalformedBytecode(unittest.TestCase):
    def test_oob_jump(self):
        # JMP 0xFFFF，远超代码段
        ins = [
            Instruction(OP_LOAD_CONST, 0, pc=0),
            Instruction(OP_JMP, 0xFFFF, pc=3),
            Instruction(OP_POP, 0, pc=6),
            Instruction(OP_RET, 0, pc=7),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[(TAG_INT, 1)], max_stack=2,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "jump.oob")
        self.assertIsNotNone(cm.exception.path)

    def test_misaligned_jump(self):
        # 跳到指令中间（LOAD_CONST 的操作数字节，offset=1）
        ins = [
            Instruction(OP_LOAD_CONST, 0, pc=0),
            Instruction(OP_JMP, 1, pc=3),
            Instruction(OP_RET, 0, pc=6),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[(TAG_INT, 1)], max_stack=2,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "jump.misaligned")

    def test_back_edge_is_allowed(self):
        # while(true) 的回边：JMP 自身，合法（但受 fuel 保护）
        ins = [Instruction(OP_JMP, 0, pc=0)]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[], max_stack=0,
        )
        verify_module(_mod(code))   # 不抛异常即通过

    def test_stack_height_merge(self):
        # 合流点栈高度不一致（手工对齐布局）：
        #  0 LOAD_CONST int1         栈 [i]
        #  3 LOAD_CONST bool true    栈 [i, cond]
        #  6 JIF 15（条件假走 else） 弹 cond, 栈 [i]
        #  9 LOAD_CONST int2         then: 栈 [i, i]
        # 12 JMP 19                  then 高度 2 到合流点
        # 15 POP                     else: 弹掉 i, 栈 []
        # 16 JMP 19                  else 高度 0 到合流点
        # 19 RET
        ins = [
            Instruction(0x10, 0, pc=0),
            Instruction(0x10, 1, pc=3),
            Instruction(0x41, 15, pc=6),
            Instruction(0x10, 2, pc=9),
            Instruction(0x40, 19, pc=12),
            Instruction(OP_POP, 0, pc=15),
            Instruction(0x40, 19, pc=16),
            Instruction(OP_RET, 0, pc=19),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins,
            consts=[(TAG_INT, 1), (TAG_BOOL, True), (TAG_INT, 2)],
            max_stack=3,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "stack.height")

    def test_stack_type_merge(self):
        # 源码级构造两个分支压入不同类型再合流使用，由编译器正常生成 CFG，
        # 验证器必须在合流点报 type.merge（借助 bool&&短路的结构不易直接造，
        # 改用手工：JIF 双分支）。
        # 0 LOAD_CONST true
        # 3 JIF 13          条件假 -> else(13)，栈空
        # 6 LOAD_CONST 7(int)   then 压 int
        # 9 JMP 16
        # 12 ??? 注意 JIF 占 3 字节，pc=6 起：
        #   6 LOAD_CONST(3B)->9, JMP(3B)->12, 13 LOAD_CONST bool(3B)->16
        ins = [
            Instruction(0x10, 0, pc=0),              # true
            Instruction(0x41, 13, pc=3),             # JIF else
            Instruction(0x10, 1, pc=6),              # int 7
            Instruction(0x40, 16, pc=9),             # JMP end
            Instruction(0x10, 2, pc=13),             # bool false (else)
            Instruction(OP_POP, 0, pc=16),           # end 合流：类型不一致
            Instruction(OP_RET, 0, pc=17),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins,
            consts=[(TAG_BOOL, True), (TAG_INT, 7), (TAG_BOOL, False)],
            max_stack=2,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "type.merge")

    def test_local_slot_oob(self):
        ins = [
            Instruction(0x11, 5, pc=0),   # LOAD_LOCAL 5，无任何槽
            Instruction(OP_POP, 0, pc=3),
            Instruction(OP_RET, 0, pc=4),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[], max_stack=1,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "local.oob")

    def test_const_oob(self):
        ins = [
            Instruction(OP_LOAD_CONST, 9, pc=0),
            Instruction(OP_POP, 0, pc=3),
            Instruction(OP_RET, 0, pc=4),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[], max_stack=1,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "const.oob")

    def test_max_stack_too_small(self):
        ins = [
            Instruction(OP_LOAD_CONST, 0, pc=0),
            Instruction(OP_LOAD_CONST, 0, pc=3),
            Instruction(OP_POP, 0, pc=6),
            Instruction(OP_POP, 0, pc=7),
            Instruction(OP_RET, 0, pc=8),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[(TAG_INT, 1)], max_stack=1,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "stack.overflow")

    def test_retv_stack_left_over(self):
        ins = [
            Instruction(OP_LOAD_CONST, 0, pc=0),
            Instruction(OP_LOAD_CONST, 0, pc=3),
            Instruction(OP_RETV, 0, pc=6),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_INT, param_tags=[], local_tags=[],
            instructions=ins, consts=[(TAG_INT, 1)], max_stack=2,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "stack.leftover")

    def test_call_unknown_index(self):
        ins = [
            Instruction(0x50, 42, pc=0),
            Instruction(OP_RET, 0, pc=3),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[], max_stack=1,
        )
        with self.assertRaises(ToolError) as cm:
            verify_module(_mod(code))
        self.assertEqual(cm.exception.kind, "call.oob")


class TestErrorPath(unittest.TestCase):
    def test_oob_path_is_shortest(self):
        ins = [
            Instruction(OP_LOAD_CONST, 0, pc=0),
            Instruction(OP_JMP, 0xFFFF, pc=3),
            Instruction(OP_RET, 0, pc=6),
        ]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[(TAG_INT, 1)], max_stack=1,
        )
        try:
            verify_module(Module([code]))
            self.fail()
        except ToolError as e:
            steps = e.path.steps
            self.assertEqual(steps[0].kind, "entry")
            self.assertEqual(steps[-1].pc, 3)

    def test_backedge_cost(self):
        from byteverifier.errorpath import build_cfg
        # JMP 自身
        ins = [Instruction(OP_JMP, 0, pc=0)]
        code = CodeObject(
            name="main", ret_tag=TAG_VOID, param_tags=[], local_tags=[],
            instructions=ins, consts=[], max_stack=0,
        )
        edges, _ = build_cfg(code)
        self.assertEqual(edges[0][0][1], "back-edge")


if __name__ == "__main__":
    unittest.main()
