"""栈式字节码验证器（本项目核心）。

验证内容
========
1. **解码合法性**：操作码合法、操作数完整、不依赖截断数据；
2. **跳转边界**：JUMP/JIF 目标在代码范围内且对齐指令边界；
3. **栈高度与类型合流**：每个基本块入口维护抽象栈（高度 + 每格
   int/bool 类型），所有前驱在合流点必须高度相等且逐格类型一致；
   算术/比较/逻辑运算按签名弹压，栈下溢立即报错；栈高超过
   ``MAX_STACK`` 报溢出；
4. **局部量初始化**：每槽状态为 uninit/int/bool，*读后写* 报错
   （LOAD uninit）。合流时一个分支已写、另一分支未写 => uninit
   （“可能未初始化”即禁止读取，与 Java/JVM 的确定赋值一致）；
   写入类型必须与槽声明类型一致；
5. **CALL/返回约定**：实参个数由被调函数签名决定、类型逐格一致；
   RET 要求空栈，RETV 要求栈顶 1 格且类型等于函数返回类型；
6. **可达性与 fall-off**：代码末尾不可“落入虚无”。

算法：单调数据流（worklist）。uninit 为初始化格的底，int/bool 互为
冲突——合流不会“提升”类型，只会保持一致或报错，因此迭代必有
上界（每槽最多 uninit->typed 一次，栈高度/类型错误立即终止）。

输出：结构化 ``VerifyError``，并附带从函数入口到错误点的
**最短错误路径**（CFG 上 BFS，路径节点映射回源码行列）。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .bytecode import (
    MAX_STACK,
    OP_ADD, OP_AND, OP_CALL, OP_DIV, OP_DUP, OP_EQ, OP_FALSE, OP_GE, OP_GT,
    OP_JIF, OP_JUMP, OP_LE, OP_LOAD, OP_LT, OP_MOD, OP_MUL, OP_NE,
    OP_NEG, OP_NOP, OP_NOT, OP_OR, OP_POP, OP_PRINT, OP_PUSH, OP_RETV,
    OP_RET, OP_STORE, OP_SUB, OP_TRUE,
    FuncCode, Instruction, Module, T_BOOL, T_INT, T_VOID,
)
from .errors import DecodeError, ErrorPathNode, VerifyError

BINOP_INT = {OP_ADD, OP_SUB, OP_MUL, OP_DIV, OP_MOD}
BINOP_CMP = {OP_EQ, OP_NE, OP_LT, OP_LE, OP_GT, OP_GE}

# 栈格类型
BOTTOM = 0          # 仅用于未初始化槽，不用于栈（栈元素总是有类型）


@dataclass
class Frame:
    """基本块入口的抽象状态。"""

    stack: list[int] = field(default_factory=list)          # T_INT / T_BOOL
    locals: list[int] = field(default_factory=list)         # BOTTOM=未赋值, 否则类型码

    def copy(self) -> "Frame":
        return Frame(list(self.stack), list(self.locals))

    def merged_stack_with(self, other: list[int]) -> tuple[list[int] | None, str | None]:
        if len(self.stack) != len(other):
            return None, (
                f"栈高度合流失败: {len(self.stack)} 与 {len(other)}")
        for k, (a, b) in enumerate(zip(self.stack, other)):
            if a != b:
                tn = {T_INT: "int", T_BOOL: "bool"}
                return None, (
                    f"栈第 {k + 1} 格（自底向顶）类型合流失败: "
                    f"{tn.get(a, a)} 与 {tn.get(b, b)}")
        return list(self.stack), None

    def merge_locals(self, other: list[int]) -> tuple[list[int] | None, str | None, int]:
        """返回 (合并结果, 错误信息, 冲突槽)。uninit 与 typed 合流为 uninit。"""
        if len(self.locals) != len(other):
            return None, f"局部槽数量不一致: {len(self.locals)} 与 {len(other)}", -1
        out: list[int] = []
        for k, (a, b) in enumerate(zip(self.locals, other)):
            if a == b:
                out.append(a)
            elif a == BOTTOM or b == BOTTOM:
                out.append(BOTTOM)
            else:
                tn = {T_INT: "int", T_BOOL: "bool"}
                return None, (
                    f"局部槽 #{k} 类型合流失败: {tn.get(a, a)} 与 {tn.get(b, b)}"), k
        return out, None, -1


@dataclass
class _SimResult:
    """从某个块头开始模拟到下一个块头/返回的结果。"""

    kind: str                 # jump | cond | ret | error
    targets: list[int] = field(default_factory=list)   # 后继 leader pc
    frame_at: dict[int, Frame] = field(default_factory=dict)  # 每个后继处的栈帧
    error: VerifyError | None = None


class Verifier:
    def __init__(self, module: Module) -> None:
        self.mod = module
        self.source_text = module.source
        self.errors: list[VerifyError] = []

    # ---------- 入口 ----------
    def verify(self) -> list[VerifyError]:
        for fi, fn in enumerate(self.mod.funcs):
            err = self._decode_err(fi, fn)
            if err is not None:
                self.errors.append(err)
                continue
            err = self._verify_func(fi, fn)
            if err is not None:
                self.errors.append(err)
        return self.errors

    def _decode_err(self, fi: int, fn: FuncCode) -> VerifyError | None:
        try:
            fn.decode()
        except DecodeError as e:
            return VerifyError(
                code="DECODE_ERROR",
                message=str(e),
                function_index=fi,
                function_name=fn.name,
                pc=-1,
            )
        return None

    # ---------- 单函数 ----------
    def _verify_func(self, fi: int, fn: FuncCode) -> VerifyError | None:
        instrs = fn.decode()
        ncode = len(fn.code)

        # 1) 线性解码已成功；计算 leader 与后继（不验证跳转参数，只看结构）。
        leaders = {0}
        pc_to_instr: dict[int, Instruction] = {}
        fallthrough: dict[int, int] = {}
        for idx, ins in enumerate(instrs):
            pc_to_instr[ins.pc] = ins
            if idx + 1 < len(instrs):
                fallthrough[ins.pc] = instrs[idx + 1].pc
            if ins.op in (OP_JUMP, OP_JIF):
                tgt = ins.target()
                if 0 <= tgt < ncode and tgt in self._known_pcs(instrs):
                    leaders.add(tgt)
            if ins.op == OP_JIF:
                if idx + 1 < len(instrs):
                    leaders.add(instrs[idx + 1].pc)
        valid_pcs = set(pc_to_instr)

        # 2) 块头帧 + worklist
        nlocals = len(fn.local_types)
        entry_frame = Frame(
            stack=[],
            locals=[fn.local_types[k] if k < len(fn.param_types) else BOTTOM
                    for k in range(nlocals)],
        )

        states: dict[int, Frame] = {0: entry_frame}
        work: deque[int] = deque([0])

        def vfail(code: str, message: str, ins: Instruction | None) -> VerifyError:
            pc = ins.pc if ins is not None else -1
            path = self._build_path(fn, pc, valid_pcs)
            sl, sc = self._src(fn, ins if ins is not None else None)
            return VerifyError(code, message, fi, fn.name, pc, sl, sc, path)

        while work:
            leader = work.popleft()
            frame = states[leader].copy()

            # 模拟从 leader 起直到本块结束（跳转/返回/下一个 leader）
            cur_pc = leader
            sim_err: VerifyError | None = None
            while True:
                ins = pc_to_instr.get(cur_pc)
                if ins is None:
                    sim_err = vfail(
                        "BAD_INSTRUCTION_BOUNDARY",
                        f"pc={cur_pc} 不是指令边界", None)
                    break

                nxt = fallthrough.get(cur_pc)
                op = ins.op

                # ---- 跳转：先做边界/对齐检查（在弹栈之前） ----
                jump_tgt = -1
                if op in (OP_JUMP, OP_JIF):
                    jump_tgt = ins.target()
                    if not (0 <= jump_tgt < ncode):
                        sim_err = vfail(
                            "JUMP_OUT_OF_BOUNDS",
                            f"{ins.name} 目标 pc={jump_tgt} 越界（代码长度 {ncode}）",
                            ins)
                        break
                    if jump_tgt not in valid_pcs:
                        sim_err = vfail(
                            "JUMP_UNALIGNED",
                            f"{ins.name} 目标 pc={jump_tgt} 没有对齐到指令边界",
                            ins)
                        break

                # ---- 抽象执行本指令（PUSH/TRUE/FALSE 在此统一处理） ----
                err = self._step(fn, ins, frame, len(self.mod.funcs))
                if err is not None:
                    sim_err = vfail(err[0], err[1], ins)
                    break

                # ---- 块终结 / 后继合流 ----
                if op == OP_JUMP:
                    sim_err = self._merge_to(states, jump_tgt, frame, ins,
                                             "jump", fn, fi, valid_pcs, work)
                    break
                if op == OP_JIF:
                    # 条件已在 _step 中弹掉；两条后继共用弹栈后的同一帧
                    if nxt is None:
                        sim_err = vfail(
                            "FALL_OFF_END",
                            "JIF 位于代码末尾，条件为假时无下一条指令可执行", ins)
                        break
                    sim_err = self._merge_to(states, jump_tgt, frame, ins,
                                             "jif-taken", fn, fi, valid_pcs, work)
                    if sim_err is not None:
                        break
                    sim_err = self._merge_to(states, nxt, frame, ins,
                                             "jif-not-taken", fn, fi,
                                             valid_pcs, work)
                    break
                if op in (OP_RET, OP_RETV):
                    break  # _step 已检查返回约定
                if nxt is None:
                    sim_err = vfail(
                        "FALL_OFF_END",
                        "控制流到达代码末尾但没有 RET/RETV/JUMP（函数必须显式返回）",
                        ins)
                    break
                if nxt in leaders:
                    sim_err = self._merge_to(states, nxt, frame, ins,
                                             "fallthrough", fn, fi,
                                             valid_pcs, work)
                    break
                cur_pc = nxt

            if sim_err is not None:
                return sim_err
        return None

    # ---------- 单条指令的抽象执行（跳转/返回/合流除外） ----------
    def _step(self, fn: FuncCode, ins: Instruction, frame: Frame,
              nfuncs: int) -> tuple[str, str] | None:
        op = ins.op
        st = frame.stack

        def pop(want: int | None = None) -> int:
            if not st:
                raise _VErr("STACK_UNDERFLOW",
                            f"{ins.name} 需要栈上至少 1 个值，但栈为空")
            t = st.pop()
            if want is not None and t != want:
                tn = {T_INT: "int", T_BOOL: "bool"}
                raise _VErr("TYPE_MISMATCH",
                            f"{ins.name} 需要 {tn[want]}，栈顶实际为 {tn[t]}")
            return t

        try:
            if op == OP_PUSH:
                self._push(frame, T_INT, ins)
                return None
            if op == OP_TRUE or op == OP_FALSE:
                self._push(frame, T_BOOL, ins)
                return None
            if op == OP_NOP:
                return None
            if op == OP_POP:
                pop()
            elif op == OP_DUP:
                if not st:
                    return ("STACK_UNDERFLOW", f"DUP 需要 1 个值，但栈为空")
                st.append(st[-1])
            elif op in BINOP_INT:
                b = pop(T_INT)
                a = pop(T_INT)
                st.append(T_INT)
            elif op in BINOP_CMP:
                pop(T_INT)
                pop(T_INT)
                st.append(T_BOOL)
            elif op == OP_AND or op == OP_OR:
                pop(T_BOOL)
                pop(T_BOOL)
                st.append(T_BOOL)
            elif op == OP_NOT:
                pop(T_BOOL)
                st.append(T_BOOL)
            elif op == OP_NEG:
                pop(T_INT)
                st.append(T_INT)
            elif op == OP_LOAD:
                slot = ins.operand
                if slot is None or slot >= len(frame.locals):
                    return ("BAD_SLOT",
                            f"LOAD 槽 #{slot} 越界（共 {len(frame.locals)} 个槽）")
                t = frame.locals[slot]
                if t == BOTTOM:
                    return ("LOCAL_UNINITIALIZED",
                            f"读取局部量槽 #{slot}（声明类型 "
                            f"{self._tn(fn.local_types[slot])}）前，"
                            f"存在一条未对其赋值的控制流路径")
                st.append(t)
            elif op == OP_STORE:
                slot = ins.operand
                if slot is None or slot >= len(frame.locals):
                    return ("BAD_SLOT",
                            f"STORE 槽 #{slot} 越界（共 {len(frame.locals)} 个槽）")
                pop(fn.local_types[slot])
                frame.locals[slot] = fn.local_types[slot]
            elif op == OP_PRINT:
                pop()       # int/bool 均可
            elif op == OP_CALL:
                callee_i = ins.operand
                if callee_i is None or callee_i >= nfuncs:
                    return ("BAD_CALL_TARGET",
                            f"CALL 函数号 {callee_i} 越界（共 {nfuncs} 个函数）")
                callee = self.mod.funcs[callee_i]
                nargs = len(callee.param_types)
                if len(st) < nargs:
                    return ("STACK_UNDERFLOW",
                            f"CALL {callee.name} 需要 {nargs} 个实参，"
                            f"栈上仅有 {len(st)} 个值")
                args = st[-nargs:]
                for k, (got, want) in enumerate(zip(args, callee.param_types)):
                    if got != want:
                        return ("TYPE_MISMATCH",
                                f"CALL {callee.name} 第 {k + 1} 个实参应为 "
                                f"{self._tn(want)}，实际为 {self._tn(got)}")
                del st[-nargs:]
                if callee.ret_type != T_VOID:
                    st.append(callee.ret_type)
            elif op == OP_RET:
                if st:
                    return ("STACK_NOT_EMPTY",
                            f"RET 要求空栈，但栈上还有 {len(st)} 个值")
                if fn.ret_type != T_VOID:
                    return ("RETURN_MISMATCH",
                            f"函数声明返回 {self._tn(fn.ret_type)}，却使用了无值 RET")
            elif op == OP_RETV:
                if not st:
                    return ("STACK_UNDERFLOW", "RETV 需要 1 个返回值，但栈为空")
                top = st[-1]
                if fn.ret_type == T_VOID:
                    return ("RETURN_MISMATCH", "void 函数不能使用带值 RETV")
                if top != fn.ret_type:
                    return ("RETURN_MISMATCH",
                            f"返回值类型 {self._tn(top)} 与声明的 "
                            f"{self._tn(fn.ret_type)} 不符")
            # JUMP/JIF 的弹栈在专门分支处理
            elif op == OP_JUMP:
                return None
            elif op == OP_JIF:
                try:
                    pop(T_BOOL)
                except _VErr as ve:
                    return (ve.code, ve.message)
            else:
                return ("UNKNOWN_OPCODE", f"未实现验证的操作码 0x{op:02x}")
        except _VErr as ve:
            return (ve.code, ve.message)

        if len(st) > MAX_STACK:
            return ("STACK_OVERFLOW",
                    f"栈高度 {len(st)} 超过上限 {MAX_STACK}")
        return None

    def _push(self, frame: Frame, t: int, ins: Instruction) -> None:
        frame.stack.append(t)
        if len(frame.stack) > MAX_STACK:
            raise _VErr("STACK_OVERFLOW",
                        f"栈高度 {len(frame.stack)} 超过上限 {MAX_STACK}")

    # ---------- 合流 ----------
    def _merge_to(self, states: dict[int, Frame], target: int,
                  incoming: Frame, ins: Instruction,
                  kind: str, fn: FuncCode, fi: int,
                  valid_pcs: set[int], work: deque[int]) -> VerifyError | None:
        existing = states.get(target)
        if existing is None:
            states[target] = incoming.copy()
            work.append(target)
            return None

        # 栈合流
        merged_stack, serr = existing.merged_stack_with(incoming.stack)
        if serr is not None:
            path = self._build_path(fn, target, valid_pcs)
            sl, sc = self._src(fn, ins)
            return VerifyError(
                "STACK_MERGE_CONFLICT",
                f"控制流在 pc={target} 合流时{serr}",
                fi, fn.name, target, sl, sc, path,
            )

        # 局部量合流
        merged_locals, lerr, slot = existing.merge_locals(incoming.locals)
        if lerr is not None:
            path = self._build_path(fn, target, valid_pcs)
            sl, sc = self._src(fn, ins)
            return VerifyError(
                "LOCAL_MERGE_CONFLICT",
                f"控制流在 pc={target} 合流时{lerr}",
                fi, fn.name, target, sl, sc, path,
            )

        # 单调更新。栈格类型合流后与某一前驱相同；局部量可能从 typed 落到
        # bottom（“可能未初始化”，信息变差），因此这里对 locals 做“发生变化
        # 即重新入队”。三态格上每个槽至多经历 bottom<->typed 的有限次变化，
        # 且首次 typed/bottom 合流即固定为 bottom，终止性有保证。
        changed = (existing.stack != merged_stack
                   or existing.locals != merged_locals)
        existing.stack = merged_stack
        existing.locals = merged_locals
        if changed and target not in work:
            work.append(target)
        return None

    # ---------- 源码映射 ----------
    def _src(self, fn: FuncCode, ins: Instruction | None) -> tuple[int | None, int | None]:
        if ins is None:
            return None, None
        pc = ins.pc
        if self.source_text:
            d = fn.debug_at(pc)
            if d is not None and 0 <= d.src_start < len(self.source_text):
                line = self.source_text.count("\n", 0, d.src_start) + 1
                line_start = self.source_text.rfind("\n", 0, d.src_start) + 1
                col = d.src_start - line_start + 1
                return line, col
        return None, None

    # ---------- 最短错误路径（BFS） ----------
    def _build_path(self, fn: FuncCode, error_pc: int,
                    valid_pcs: set[int]) -> list[ErrorPathNode]:
        """在函数的结构化 CFG 上做 BFS，找入口(pc=0) -> 错误指令的最短路径。

        仅沿“合法边”搜索（跳转目标通过边界/对齐检查），因此即便函数里别处
        还埋着坏跳转，路径本身也是一条执行时真的可能走到的路径。
        边分为 jump / jif-taken / jif-not-taken / fallthrough，
        每个节点带 pc 与源码行列，便于阅读。
        """
        if error_pc < 0 or not valid_pcs:
            return []
        instrs = fn.decode()
        pc_to_ins = {i.pc: i for i in instrs}
        next_pc = {instrs[i].pc: instrs[i + 1].pc
                   for i in range(len(instrs) - 1)}

        def succs(pc: int) -> list[tuple[int, str]]:
            ins = pc_to_ins[pc]
            out: list[tuple[int, str]] = []
            if ins.op == OP_JUMP:
                t = ins.target()
                if t in valid_pcs:
                    out.append((t, "jump"))
            elif ins.op == OP_JIF:
                t = ins.target()
                if t in valid_pcs:
                    out.append((t, "jif-taken"))
                if pc in next_pc:
                    out.append((next_pc[pc], "jif-not-taken"))
            elif ins.op not in (OP_RET, OP_RETV) and pc in next_pc:
                out.append((next_pc[pc], "fallthrough"))
            return out

        goal = error_pc
        if goal == 0:
            sl0, sc0 = self._src(fn, pc_to_ins.get(0))
            return [ErrorPathNode(pc=0, kind="entry", target=0,
                                  src_line=sl0, src_col=sc0, note=fn.name)]

        prev: dict[int, tuple[int, str]] = {}
        seen = {0}
        q = deque([0])
        found = False
        while q and not found:
            pc = q.popleft()
            for t, kind in succs(pc):
                if t in seen:
                    continue
                seen.add(t)
                prev[t] = (pc, kind)
                if t == goal:
                    found = True
                    break
                q.append(t)

        if not found:
            # 错误点在结构上不可由入口到达（例如它本身位于死代码）：
            # 给出入口单点，错误信息仍指明 pc。
            sl0, sc0 = self._src(fn, pc_to_ins.get(0))
            return [ErrorPathNode(pc=0, kind="entry", target=0,
                                  src_line=sl0, src_col=sc0, note=fn.name)]

        # 回溯出边链
        chain: list[tuple[int, str]] = []
        cur = goal
        while cur != 0:
            p, kind = prev[cur]
            chain.append((p, kind))
            cur = p
        chain.reverse()

        nodes: list[ErrorPathNode] = []
        sl0, sc0 = self._src(fn, pc_to_ins.get(0))
        nodes.append(ErrorPathNode(pc=0, kind="entry", target=0,
                                   src_line=sl0, src_col=sc0, note=fn.name))
        for from_pc, kind in chain:
            ins = pc_to_ins[from_pc]
            if kind in ("jump", "jif-taken"):
                t = ins.target()
            else:
                t = next_pc.get(from_pc)
            sl, sc = self._src(fn, ins)
            nodes.append(ErrorPathNode(
                pc=from_pc, kind=kind, target=t, src_line=sl, src_col=sc))
        return nodes

    @staticmethod
    def _known_pcs(instrs: list[Instruction]) -> set[int]:
        return {i.pc for i in instrs}

    @staticmethod
    def _tn(code: int) -> str:
        return {T_INT: "int", T_BOOL: "bool", T_VOID: "void"}.get(code, str(code))


class _VErr(Exception):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def verify_module(module: Module) -> list[VerifyError]:
    return Verifier(module).verify()
