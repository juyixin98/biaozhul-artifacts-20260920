"""栈式字节码静态验证器（项目核心）。

每个函数做一次基于抽象状态的数据流验证，保证通过验证的代码：

1. **跳转边界** —— 所有跳转目标在代码段内且落在指令边界上；回边/前向边均检查。
2. **栈高度** —— 每条指令入口栈深度非负（不会下溢），且合流点的栈高度一致；
   声明的 ``max_stack`` 不小于实际最大深度（不会上溢）。
3. **类型合流** —— 合流点栈上逐槽类型一致；算术/比较/取反/分支弹出的值类型正确；
   实参与形参类型一致；RET(V) 与函数返回类型一致。
4. **局部量初始化** —— 读取局部量时，在到达该读取的每条控制流路径上都已初始化。

关键不变式：**通过验证的函数在解释器中不可能发生操作数栈下溢**（每一步的
弹出操作在抽象执行时都已校验深度与类型）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from . import bytecode as bc
from .bytecode import (
    BUILTIN_PRINT_BOOL, BUILTIN_PRINT_INT, JUMP_OPS, OPERAND_SIZE, TAG_BOOL,
    TAG_INT, TAG_NAMES, TAG_VOID, CodeObject, Module,
)
from .common import PHASE_VERIFIER, ToolError
from .errorpath import ErrorPath, shortest_path

BOTTOM = 0   # 局部量“未初始化”标记

# 二元运算：(操作码,) -> (期望操作数类型, 结果类型)
_BIN_INT_INT = {
    bc.OP_ADD: TAG_INT, bc.OP_SUB: TAG_INT, bc.OP_MUL: TAG_INT, bc.OP_DIV: TAG_INT,
}
_BIN_CMP = {
    bc.OP_EQ: None, bc.OP_NE: None, bc.OP_LT: None,
    bc.OP_GT: None, bc.OP_LE: None, bc.OP_GE: None,
}  # 操作数类型必须相同


@dataclass
class Frame:
    stack: list[int] = field(default_factory=list)
    locals: list[int] = field(default_factory=list)   # BOTTOM 表示未初始化

    def copy(self) -> "Frame":
        return Frame(list(self.stack), list(self.locals))


def verify_module(module: Module, require_main: bool = False) -> None:
    """验证整个模块。发现第一个错误即抛 :class:`ToolError`。"""
    seen: set[str] = set()
    for f in module.functions:
        if f.name in seen:
            raise ToolError(
                PHASE_VERIFIER, "dup.function", f"函数名 {f.name!r} 重复",
                func_name=f.name,
            )
        seen.add(f.name)

    for f in module.functions:
        _verify_function(f, module)

    if require_main:
        main = module.by_name("main")
        if main is None:
            raise ToolError(
                PHASE_VERIFIER, "missing.main",
                "可运行模块必须包含 main 函数（无参数）",
            )
        if main.param_tags:
            raise ToolError(
                PHASE_VERIFIER, "main.params",
                "main 函数必须无参数", func_name="main",
            )


# ---------- 单个函数 ----------

def _verify_function(code: CodeObject, module: Module) -> None:
    ins_list = code.instructions
    if not ins_list:
        raise ToolError(
            PHASE_VERIFIER, "empty.code", "函数体为空（至少需要一条返回指令）",
            func_name=code.name, pc=0,
        )
    by_pc = {ins.pc: ins for ins in ins_list}
    boundaries = set(by_pc)
    # 依据实际指令布局算出“指令流的自然结束位置”。
    # 变异可能把声明的 code_len 改大/截断：解码出的指令只是自然布局的前缀，
    # 真实边界检查必须以自然布局为准，不能信任头里的 code_len。
    natural_len = ins_list[-1].pc + 1 + OPERAND_SIZE[ins_list[-1].opcode]

    # ---- 0) 声明的 code_len 必须与指令自然布局一致（防截断/填充变异） ----
    if code.declared_code_len and code.declared_code_len != natural_len:
        raise ToolError(
            PHASE_VERIFIER, "code.length",
            f"头部声明代码段长度 {code.declared_code_len}，"
            f"但指令自然布局长度为 {natural_len}（代码段被截断或填充）",
            func_name=code.name, pc=0,
        )
    code_len = natural_len

    # ---- 1) 静态：跳转边界（回边/前向边统一处理） ----
    for ins in ins_list:
        if ins.opcode in JUMP_OPS:
            tgt = ins.operand
            if tgt > code_len:
                raise _verr(
                    code, ins, "jump.oob",
                    f"跳转到 offset={tgt}，但代码段长度只有 {code_len}（越界跳转）",
                )
            if tgt != code_len and tgt not in boundaries:
                raise _verr(
                    code, ins, "jump.misaligned",
                    f"跳转目标 offset={tgt} 不在指令边界上"
                    f"（合法边界: {sorted(boundaries)}）",
                )
            if tgt == ins.pc:
                # 跳自身是合法的回边（例如 while(true)），不拦
                pass

    # 静态：立即数范围
    nslots = code.nslots
    for ins in ins_list:
        if ins.opcode in (bc.OP_LOAD_LOCAL, bc.OP_STORE_LOCAL):
            if not (0 <= ins.operand < nslots):
                raise _verr(
                    code, ins, "local.oob",
                    f"局部槽下标 {ins.operand} 越界（本函数共 {nslots} 个槽）",
                )
        if ins.opcode == bc.OP_LOAD_CONST:
            if not (0 <= ins.operand < len(code.consts)):
                raise _verr(
                    code, ins, "const.oob",
                    f"常量池下标 {ins.operand} 越界（共 {len(code.consts)} 项）",
                )
        if ins.opcode == bc.OP_CALL:
            _callee_sig(ins.operand, module)  # 仅校验调用编号存在

    # ---- 2) 数据流不动点 ----
    entry = Frame(
        stack=[],
        locals=list(code.param_tags) + [BOTTOM] * len(code.local_tags),
    )
    in_states: dict[int, Frame] = {0: entry}
    worklist = deque([0])
    observed_max = 0
    reaches_end = False
    end_predecessors: list[int] = []   # 能顺序/分支走到函数末尾的指令 pc

    def merge(pc: int, fr: Frame) -> Frame | None:
        """把 fr 合流到 pc 的入口状态；有变化返回旧状态拷贝（用于继续传播）。"""
        old = in_states.get(pc)
        if old is None:
            in_states[pc] = fr.copy()
            return in_states[pc]
        changed = _merge_into(old, fr, pc)
        return old if changed else None

    def _merge_into(old: Frame, other: Frame, pc: int) -> bool:
        # 栈高度必须一致
        if len(old.stack) != len(other.stack):
            raise _verr(
                code, by_pc[pc], "stack.height",
                f"合流点 offset={pc} 栈高度不一致: {len(old.stack)} vs "
                f"{len(other.stack)}",
            )
        changed = False
        # 栈上逐槽合流：具体类型必须相同
        for i, (a, b) in enumerate(zip(old.stack, other.stack)):
            if a != b:
                raise _verr(
                    code, by_pc[pc], "type.merge",
                    f"合流点 offset={pc} 栈第 {i + 1} 个值类型不一致: "
                    f"{TAG_NAMES.get(a, a)} vs {TAG_NAMES.get(b, b)}",
                )
        # 局部量：声明类型相同；初始化保守合流（任一前驱未初始化 => 未初始化）
        for i, (a, b) in enumerate(zip(old.locals, other.locals)):
            m = a if a == b else BOTTOM
            if old.locals[i] != m:
                old.locals[i] = m
                changed = True
        return changed

    while worklist:
        pc = worklist.popleft()
        ins = by_pc[pc]
        fr = in_states[pc].copy()
        observed_max = max(observed_max, len(fr.stack))

        # ---- 指令语义（带栈/类型检查） ----
        op = ins.opcode

        def need(n: int) -> None:
            if len(fr.stack) < n:
                raise _verr(
                    code, ins, "stack.underflow",
                    f"指令 {bc.OPCODE_NAMES[op]} 需要 {n} 个栈值，"
                    f"但栈上只有 {len(fr.stack)} 个",
                )

        def pop_expect(tag: int, what: str) -> int:
            need(1)
            got = fr.stack.pop()
            if got != tag:
                raise _verr(
                    code, ins, "type.operand",
                    f"{what}需要 {TAG_NAMES[tag]}，但栈顶是 {TAG_NAMES.get(got, got)}",
                )
            return got

        def push(tag: int) -> None:
            fr.stack.append(tag)
            if len(fr.stack) > code.max_stack:
                raise _verr(
                    code, ins, "stack.overflow",
                    f"压栈后深度 {len(fr.stack)} 超过声明的 max_stack="
                    f"{code.max_stack}",
                )

        if op == bc.OP_LOAD_CONST:
            tag = code.consts[ins.operand][0]
            push(tag)

        elif op == bc.OP_LOAD_LOCAL:
            slot = ins.operand
            tag = fr.locals[slot]
            if tag == BOTTOM:
                sname = (
                    code.slot_names[slot]
                    if slot < len(code.slot_names) else f"slot{slot}"
                )
                raise _verr(
                    code, ins, "local.uninit",
                    f"读取局部量 {sname!r}，但存在未对其初始化即到达此处的路径",
                )
            push(tag)

        elif op == bc.OP_STORE_LOCAL:
            need(1)
            val_tag = fr.stack.pop()
            decl_tag = fr.locals[ins.operand] if fr.locals[ins.operand] != BOTTOM \
                else (code.param_tags + code.local_tags)[ins.operand]
            if val_tag != decl_tag:
                raise _verr(
                    code, ins, "type.local",
                    f"向声明为 {TAG_NAMES[decl_tag]} 的局部槽写入 "
                    f"{TAG_NAMES.get(val_tag, val_tag)}",
                )
            fr.locals[ins.operand] = val_tag

        elif op == bc.OP_POP:
            need(1)
            fr.stack.pop()

        elif op in _BIN_INT_INT:
            need(2)
            b = fr.stack.pop(); a = fr.stack.pop()
            if a != TAG_INT or b != TAG_INT:
                raise _verr(
                    code, ins, "type.operand",
                    f"{bc.OPCODE_NAMES[op]} 的两个操作数都必须是 int，得到 "
                    f"{TAG_NAMES.get(a, a)} 和 {TAG_NAMES.get(b, b)}",
                )
            push(TAG_INT)

        elif op in _BIN_CMP:
            need(2)
            b = fr.stack.pop(); a = fr.stack.pop()
            if a != b or a not in (TAG_INT, TAG_BOOL):
                raise _verr(
                    code, ins, "type.operand",
                    f"{bc.OPCODE_NAMES[op]} 要求两个同类型 (int/int 或 bool/bool) "
                    f"操作数，得到 {TAG_NAMES.get(a, a)} 和 {TAG_NAMES.get(b, b)}",
                )
            push(TAG_BOOL)

        elif op == bc.OP_NEG:
            pop_expect(TAG_INT, "NEG ")
            push(TAG_INT)
        elif op == bc.OP_NOT:
            pop_expect(TAG_BOOL, "NOT ")
            push(TAG_BOOL)

        elif op in JUMP_OPS:
            if op == bc.OP_JIF:
                pop_expect(TAG_BOOL, "条件分支 ")
            nxt = pc + 1 + OPERAND_SIZE[op]
            raw_targets: list[int] = [ins.operand]
            if op == bc.OP_JIF and nxt != ins.operand and nxt <= code_len:
                raw_targets.append(nxt)
            for t in raw_targets:
                if t == code_len:
                    reaches_end = True
                    end_predecessors.append(pc)
                    continue
                if t not in boundaries:
                    raise _verr(
                        code, ins, "jump.misaligned",
                        f"跳转目标 offset={t} 不是指令边界",
                    )
                got = merge(t, fr)
                if got is not None:
                    worklist.append(t)
            continue

        elif op == bc.OP_CALL:
            ptags, rtag = _callee_sig(ins.operand, module)
            if len(fr.stack) < len(ptags):
                raise _verr(
                    code, ins, "stack.underflow",
                    f"调用需要 {len(ptags)} 个实参，栈上只有 {len(fr.stack)} 个",
                )
            actual = fr.stack[-len(ptags):] if ptags else []
            for i, (want, got) in enumerate(zip(ptags, actual)):
                if want != got:
                    raise _verr(
                        code, ins, "type.arg",
                        f"第 {i + 1} 个实参应为 {TAG_NAMES[want]}，"
                        f"实际为 {TAG_NAMES.get(got, got)}",
                    )
            if ptags:
                del fr.stack[-len(ptags):]
            if rtag != TAG_VOID:
                push(rtag)

        elif op == bc.OP_RETV:
            pop_expect(code.ret_tag, "返回值 ")
            if fr.stack:
                raise _verr(
                    code, ins, "stack.leftover",
                    f"RETV 时栈上还残留 {len(fr.stack)} 个值",
                )
            continue

        elif op == bc.OP_RET:
            if code.ret_tag != TAG_VOID:
                raise _verr(
                    code, ins, "return.value",
                    f"返回类型为 {TAG_NAMES[code.ret_tag]} 的函数不能用无值 RET",
                )
            if fr.stack:
                raise _verr(
                    code, ins, "stack.leftover",
                    f"RET 时栈上还残留 {len(fr.stack)} 个值",
                )
            continue

        else:  # pragma: no cover - 解码阶段已拦非法操作码
            raise _verr(code, ins, "opcode", f"非法操作码 0x{op:02x}")

        # 顺序后继
        nxt = pc + 1 + OPERAND_SIZE[op]
        if nxt in boundaries:
            got = merge(nxt, fr)
            if got is not None:
                worklist.append(nxt)
        elif nxt == code_len:
            reaches_end = True
            end_predecessors.append(pc)
        else:
            raise _verr(
                code, ins, "fallthrough.oob",
                f"offset={pc} 顺序落到 offset={nxt}，不是指令边界且超出代码段",
            )

    # ---- 3) 终结性：任何函数都不允许控制流走到代码段末尾 ----
    # （void 的隐式 RET 由编译器补；落到末尾通常是截断变异/异常返回）
    if reaches_end:
        anchor_pc = max(end_predecessors) if end_predecessors else ins_list[-1].pc
        anchor = by_pc[anchor_pc]
        if code.ret_tag != TAG_VOID:
            raise _verr(
                code, anchor, "return.missing",
                f"函数 {code.name!r} 声明返回 {TAG_NAMES[code.ret_tag]}，"
                "但存在不经过 RETV 直接走到函数末尾的路径（异常返回：缺少返回值）",
            )
        raise _verr(
            code, anchor, "ret.missing",
            f"函数 {code.name!r} 存在直接走到函数末尾、没有经过 RET 的路径",
        )

    # ---- 4) max_stack 复核 ----
    if code.max_stack < observed_max:
        first = ins_list[0]
        raise _verr(
            code, first, "maxstack.small",
            f"声明 max_stack={code.max_stack}，实际可达最大栈深度 {observed_max}",
        )
    if code.max_stack > 255:
        raise _verr(
            code, ins_list[0], "maxstack.large",
            f"max_stack={code.max_stack} 超过 255",
        )


def _callee_sig(idx: int, module: Module):
    if idx in (BUILTIN_PRINT_INT, BUILTIN_PRINT_BOOL):
        _, ptags, rtag = {
            BUILTIN_PRINT_INT: ("print_int", [TAG_INT], TAG_VOID),
            BUILTIN_PRINT_BOOL: ("print_bool", [TAG_BOOL], TAG_VOID),
        }[idx]
        return ptags, rtag
    if 0 <= idx < len(module.functions):
        f = module.functions[idx]
        return list(f.param_tags), f.ret_tag
    raise ToolError(
        PHASE_VERIFIER, "call.oob",
        f"CALL 目标编号 {idx} 既不是用户函数也不是内建函数",
    )


def _verr(code: CodeObject, ins, kind: str, msg: str) -> ToolError:
    """构造验证错误，并附带入口到该指令的最短路径。"""
    path = shortest_path(code, ins.pc)
    span = code.pc_spans.get(ins.pc)
    return ToolError(
        PHASE_VERIFIER, kind, msg, span=span, pc=ins.pc,
        func_name=code.name, path=path,
    )
