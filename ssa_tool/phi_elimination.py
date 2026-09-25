"""SSA 回退（φ 消除）：SSA IR -> 无 φ 的可执行 IR。

做法（标准“φ 目的物位置化”方案）：

1. **为每个 φ 目的分配一个可写寄存器** ``r<N>``。SSA 值不可变，而 φ 的值在
   每次沿不同边进入时不同，因此必须有独立的可变位置；
2. **全函数改写**：所有把 φ 目的名作为操作数的使用（普通指令、终结指令、
   其它 φ 的入边值）都改成对应的 ``r<N>``；
3. **按边并行复制**：对边 ``p -> b``，b 的每个 φ 生成
   ``r_dest <- 入边值（经映射）``；
4. **分裂关键边**：源块出度 > 1、目标块入度 > 1 时插入 ``@split.N``，
   复制放到新块；否则复制放在前驱末尾；
5. **串行化**：:mod:`parallel_copy` 处理交换环（如循环 latch 上
   r_a <- r_b、r_b <- r_a）。
"""

from __future__ import annotations

from .errors import SSAError
from .ir import Block, FunctionIR, Instr, Name, Operand
from .parallel_copy import sequentialize


class PhiEliminator:
    def __init__(self, fn: FunctionIR):
        if fn.flavor != "ssa":
            raise SSAError(f"φ 消除要求 SSA IR，得到 {fn.flavor}")
        self.fn = fn
        self.split_counter = 0
        self.loc_of_phi: dict[str, str] = {}     # φ 目的 -> 可写寄存器
        self.edge_pairs: dict[tuple[str, str], list[tuple[str, Operand]]] = {}
        self.extra: list[Block] = []

    # ---------------- 位置分配 ----------------

    def _assign_locations(self) -> None:
        n = 0
        for b in self.fn.blocks:
            for phi in b.phis:
                self.loc_of_phi[phi.dest] = f"r{n}"
                n += 1

    def _use(self, op: Operand) -> Operand:
        if isinstance(op, Name) and op.name in self.loc_of_phi:
            return Name(self.loc_of_phi[op.name])
        return op

    def _rewrite_uses(self) -> None:
        for b in self.fn.blocks:
            for phi in b.phis:
                phi.incoming = {p: self._use(v) for p, v in phi.incoming.items()}
            for ins in b.instrs:
                ins.operands = [self._use(o) for o in ins.operands]
            if b.terminator is not None:
                b.terminator.operands = [
                    self._use(o) for o in b.terminator.operands]
        # φ 目的本身改名（后续复制的目标寄存器名）
        for b in self.fn.blocks:
            for phi in b.phis:
                phi.dest = self.loc_of_phi[phi.dest]

    # ---------------- 边上的并行复制 ----------------

    def _collect(self) -> None:
        preds = self.fn.preds()
        for b in self.fn.blocks:
            if not b.phis:
                continue
            for p in preds.get(b.name, []):
                pairs: list[tuple[str, Operand]] = []
                for phi in b.phis:
                    if p not in phi.incoming:
                        raise SSAError(
                            f"φ %{phi.dest} @{b.name} 缺前驱 @{p} 的入边")
                    pairs.append((phi.dest, phi.incoming[p]))
                self.edge_pairs[(p, b.name)] = pairs

    def _emit(self) -> None:
        preds = self.fn.preds()
        by_name = {b.name: b for b in self.fn.blocks}
        for (p, t), pairs in self.edge_pairs.items():
            pb = by_name[p]
            moves = sequentialize(pairs)
            instrs = [Instr("copy", m.dest, [m.src]) for m in moves]
            critical = len(pb.successors()) > 1 and len(preds.get(t, [])) > 1
            if critical:
                sb = Block(self._split_name())
                sb.instrs = instrs
                sb.terminator = Instr("jmp", None, blocks=[t])
                self.extra.append(sb)
                self._redirect(pb, t, sb.name)
            else:
                pb.instrs.extend(instrs)

    def _split_name(self) -> str:
        n = self.split_counter
        self.split_counter += 1
        return f"split.{n}"

    @staticmethod
    def _redirect(pb: Block, old: str, via: str) -> None:
        t = pb.terminator
        if t is None:
            raise SSAError(f"@{pb.name} 无终结指令")
        if t.op == "jmp":
            if t.blocks[0] != old:
                raise SSAError("jmp 重定向目标不匹配")
            t.blocks = [via]
        elif t.op == "br":
            t.blocks = [via if x == old else x for x in t.blocks]
        else:
            raise SSAError(f"无法从 {t.op} 分裂关键边")

    # ---------------- 收尾 ----------------

    def _strip(self) -> None:
        for b in self.fn.blocks:
            b.phis = []

    def _place_splits(self) -> None:
        split_names = {sb.name for sb in self.extra}
        after: dict[str, list[Block]] = {}
        for b in self.fn.blocks:
            for s in b.successors():
                if s in split_names:
                    sb = next(x for x in self.extra if x.name == s)
                    after.setdefault(b.name, []).append(sb)
        final: list[Block] = []
        for b in self.fn.blocks:
            final.append(b)
            final.extend(after.get(b.name, []))
        self.fn.blocks = final

    def _validate(self) -> None:
        labels = {b.name for b in self.fn.blocks}
        for b in self.fn.blocks:
            if b.phis:
                raise SSAError(f"回退后 @{b.name} 仍有 φ")
            if b.terminator is None:
                raise SSAError(f"@{b.name} 缺终结指令")
            for s in b.successors():
                if s not in labels:
                    raise SSAError(f"@{b.name} 跳转到不存在的 @{s}")

    def eliminate(self) -> FunctionIR:
        self._assign_locations()
        self._rewrite_uses()
        self._collect()
        self._emit()
        self._strip()
        self._place_splits()
        self.fn.flavor = "exec"
        self._validate()
        return self.fn


def eliminate_phis(fn: FunctionIR) -> FunctionIR:
    return PhiEliminator(fn).eliminate()
