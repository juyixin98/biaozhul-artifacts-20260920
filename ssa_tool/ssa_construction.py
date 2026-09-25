"""SSA 构造：内存风格 IR -> SSA IR（Cytron 风格 mem2reg）。

两阶段：

1. **插入 φ 指令**：对每个 ``alloc`` 槽，沿定义块的支配边界放置 φ；
2. **重命名**：沿支配树 DFS，为每个槽维护版本栈：
   - ``alloc``         → 该槽入口定义 ``<slot>.0``（const 初值），压栈；
   - ``store v, slot`` → 生成**全新 SSA 名字** ``<slot>.<k>``（一条新的
     普通 ``copy`` 定义，语义即“本块内 slot 的新版本”），把 v 记入该名字
     并压栈——这样同一变量多次赋值产生多个不同 SSA 名；
   - ``load slot``     → 整个指令删除，结果名映射为栈顶 SSA 值；
   - 普通指令          → 操作数经映射后重写；
   - 块结束时          → 为每个后继 φ 按边填进入值；
   - 离开块            → 弹栈。

**不可达块**：不插 φ、不参与重命名 DFS。单独隔离处理：每个外部引用本地落地为
独立 ``const 0``，临时值改名 ``u<N>``，保证“每个名字一处定义”；
指向可达块的 φ 入边统一补 ``const 0``。
"""

from __future__ import annotations

from collections import defaultdict

from .dominance import DomInfo, analyze, dominance_frontiers, dominator_tree
from .errors import SSAError
from .ir import Block, Const, FunctionIR, Instr, Name, Operand, PhiInstr

ZERO = Const(0)


class SSAConstructor:
    def __init__(self, fn: FunctionIR):
        self.raw = fn
        self.dom: DomInfo = analyze(fn)
        self.preds_raw = fn.preds()

        self.slots: list[str] = []
        self.slot_set: set[str] = set()
        self.def_blocks: dict[str, list[str]] = defaultdict(list)
        self.phis: dict[str, list[PhiInstr]] = defaultdict(list)

        self.stacks: dict[str, list[str]] = {}    # 槽 -> SSA 名字栈
        self.values: dict[str, Operand] = {}     # SSA 名字 -> 值
        self.counter: dict[str, int] = {}
        self.name_map: dict[str, Operand] = {}   # raw 临时名 -> SSA 操作数

        self.new_blocks: list[Block] = []

    # ===================== 预处理 =====================

    def _collect_slots(self) -> None:
        for b in self.raw.blocks:
            for ins in b.instrs:
                if ins.op == "alloc":
                    if ins.dest in self.slot_set:
                        raise SSAError(f"槽 %{ins.dest} 重复 alloc")
                    self.slot_set.add(ins.dest)
                    self.slots.append(ins.dest)
        # 防御：被 store 但无 alloc 的槽（正常构建器不产生）
        for ins in self.raw.entry.instrs:
            if (ins.op == "store" and ins.slot is not None
                    and ins.slot not in self.slot_set):
                self.slot_set.add(ins.slot)
                self.slots.append(ins.slot)
        for s in self.slots:
            self.stacks[s] = []
            # 版本 0 保留给入口 alloc 的 const；φ 与各次 store 从 1 起编号
            self.counter[s] = 1
        for b in self.raw.blocks:
            if b.name not in self.dom.reachable:
                continue
            for ins in b.instrs:
                if ins.op == "store" and ins.slot in self.slot_set:
                    self.def_blocks[ins.slot].append(b.name)

    # ===================== φ 插入 =====================

    def _insert_phis(self) -> None:
        df = dominance_frontiers(self.dom.order, self.dom.idom, self.dom.preds)
        for slot in self.slots:
            worklist = list(dict.fromkeys(self.def_blocks.get(slot, [])))
            seen: set[str] = set()
            while worklist:
                x = worklist.pop()
                for y in df.get(x, ()):
                    if y in seen:
                        continue
                    seen.add(y)
                    dest = self._new_name(slot)
                    self.phis[y].append(PhiInstr(dest, slot, {}))
                    if y not in self.def_blocks.get(slot, []):
                        worklist.append(y)

    def _new_name(self, slot: str) -> str:
        i = self.counter[slot]
        self.counter[slot] = i + 1
        return f"{slot}.{i}"

    # ===================== 重命名（可达块） =====================

    def _rename(self, block_name: str) -> None:
        rb = self.raw.block(block_name)
        nb = Block(block_name)
        pushed: list[str] = []          # 本块压栈的槽名

        for phi in self.phis.get(block_name, []):
            nb.phis.append(phi)
            self.stacks[phi.slot].append(phi.dest)
            self.values[phi.dest] = Name(phi.dest)
            pushed.append(phi.slot)

        for ins in rb.instrs:
            out = self._translate(ins, pushed)
            if out is not None:
                nb.instrs.append(out)

        term = self._translate_terminator(rb)
        if term is None:
            raise SSAError(f"可达块 @{block_name} 缺少终结指令")
        nb.terminator = term
        self.new_blocks.append(nb)

        for s in rb.successors():
            if s not in self.dom.reachable:
                continue
            for phi in self.phis.get(s, []):
                st = self.stacks[phi.slot]
                # 入边值必须是“出口处槽内容”对应的 SSA 名字本身；
                # 不能展开成它 copy 的源（那个名字可能随迭代重新定义）。
                top = st[-1] if st else None
                phi.incoming[block_name] = (
                    Name(top) if top is not None else ZERO)

        for child in dominator_tree(self.dom.idom).get(block_name, []):
            self._rename(child)

        for slot in pushed:
            self.stacks[slot].pop()

    def _map(self, op: Operand) -> Operand:
        if isinstance(op, Const):
            return op
        return self.name_map.get(op.name, op)

    def _translate(self, ins: Instr, pushed: list[str]) -> Instr | None:
        if ins.op == "alloc":
            slot = ins.dest
            init = self._map(ins.operands[0]) if ins.operands else ZERO
            name = slot + ".0"
            out = Instr("const", name, [init], span=ins.span)
            self.values[name] = Name(name)
            self.stacks[slot].append(name)
            pushed.append(slot)
            return out

        if ins.op == "store":
            slot = ins.slot
            val = self._map(ins.operands[0])
            new_name = self._new_name(slot)
            # 新 SSA 名字；值为所存内容，用一条 copy 承载（后续死存储可消除，
            # 这里保留以获得“每个名字一处定义”的直观形式）。
            out = Instr("copy", new_name, [val], span=ins.span)
            self.values[new_name] = val
            self.stacks[slot].append(new_name)
            pushed.append(slot)
            return out

        if ins.op == "load":
            slot = ins.slot
            st = self.stacks[slot]
            top = st[-1] if st else None
            self.name_map[ins.dest] = (
                self.values[top] if top is not None else ZERO)
            return None

        ops = [self._map(o) for o in ins.operands]
        out = Instr(ins.op, ins.dest, ops, list(ins.blocks), ins.slot, ins.span)
        if ins.dest is not None:
            self.name_map[ins.dest] = Name(ins.dest)
        return out

    def _translate_terminator(self, rb: Block) -> Instr | None:
        t = rb.terminator
        if t is None:
            return None
        ops = [self._map(o) for o in t.operands]
        return Instr(t.op, t.dest, ops, list(t.blocks), t.slot, t.span)

    # ===================== 不可达块隔离处理 =====================

    def _process_unreachable(self) -> None:
        counter = [0]

        def fresh() -> str:
            n = f"u{counter[0]}"
            counter[0] += 1
            return n

        for rb in self.raw.blocks:
            if rb.name in self.dom.reachable:
                continue
            nb = Block(rb.name)
            local: dict[str, Operand] = {}

            def isolated(op: Operand, span) -> Operand:
                if isinstance(op, Const):
                    return op
                if op.name in local:
                    return local[op.name]
                d = fresh()
                nb.instrs.append(Instr("const", d, [ZERO], span=span))
                local[op.name] = Name(d)
                return Name(d)

            for ins in rb.instrs:
                if ins.op in ("alloc",):
                    init = isolated(ins.operands[0] if ins.operands else ZERO,
                                   ins.span)
                    nb.instrs.append(Instr("const", ins.dest + ".0", [init],
                                           span=ins.span))
                    local[ins.dest] = Name(ins.dest + ".0")
                elif ins.op == "store":
                    local[ins.slot] = isolated(ins.operands[0], ins.span)
                elif ins.op == "load":
                    local[ins.dest] = local.get(ins.slot, ZERO)
                else:
                    ops = [isolated(o, ins.span) for o in ins.operands]
                    nb.instrs.append(Instr(ins.op, ins.dest, ops,
                                          list(ins.blocks), ins.slot, ins.span))
                    if ins.dest is not None:
                        local[ins.dest] = Name(ins.dest)
            if rb.terminator is not None:
                t = rb.terminator
                ops = [isolated(o, t.span) for o in t.operands]
                nb.terminator = Instr(t.op, t.dest, ops, list(t.blocks),
                                     t.slot, t.span)
            self.new_blocks.append(nb)

    def _fill_missing_incoming(self) -> None:
        for s in self.dom.reachable:
            for phi in self.phis.get(s, []):
                for p in self.preds_raw.get(s, []):
                    if p not in self.dom.reachable and p not in phi.incoming:
                        phi.incoming[p] = ZERO

    # ===================== 入口 =====================

    def construct(self) -> FunctionIR:
        self._collect_slots()
        self._insert_phis()
        if self.raw.entry.name not in self.dom.reachable:
            raise SSAError("入口块不可达，IR 结构非法")
        self._rename(self.raw.entry.name)
        self._process_unreachable()
        self._fill_missing_incoming()

        order = {b.name: i for i, b in enumerate(self.raw.blocks)}
        self.new_blocks.sort(key=lambda b: order.get(b.name, 1 << 30))
        return FunctionIR(self.raw.name, list(self.raw.params),
                          self.new_blocks, flavor="ssa")


def construct_ssa(fn: FunctionIR) -> FunctionIR:
    return SSAConstructor(fn).construct()
