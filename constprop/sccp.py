"""稀疏条件常量传播（Sparse Conditional Constant Propagation, SCCP）。

实现 Wegman & Zadeck, *Constant Propagation with Conditional Branches*
(1991) 的经典算法。

格
--
每个 SSA 值取四种状态之一：

==================  ===========================================
``top``（未定义）   尚未被任何*已执行*定义求值；φ 的一条尚未执行的
                    入边也按 top 处理（SCCP 的乐观假设）
``const(n)``        所有已执行定义一致给出常量 n
``undef``           已执行定义显式产生的未定义值（读未初始化变量，
                    或其沿纯运算传播的结果）——是一种“确定的 poison”，
                    合流时**不会**被常量吸收
``bottom``（非常量）至少一次定义不是常量，或合流出不同常量
==================  ===========================================

关键区分 ``top`` 与 ``undef``：

- ``top`` 是“还不知道”——典型是暂时不可执行的分支 / φ 入边，应乐观地
  被另一条已知常量边吸收（``top ∧ const(n) = const(n)``）；
- ``undef`` 是“在某条**可执行**路径上确实产生了未定义值”，必须保留：
  ``undef ∧ const(n) = undef``。若错误地把它当 top，``0||undef`` 之类
  短路结果会被错误折叠成常量，从而改变未定义错误的可观察行为。

两个工作流同时推进（“稀疏 + 条件”的关键）
-----------------------------------------

- CFG 边工作流：一条边只在其条件（若有）被格信息证实时才变“可执行”；
  只有可达块参与求值，于是不可达分支里的指令不能污染格。
- SSA 工作流：一个值的格一旦下降，仅重新求值直接使用它的指令
  （稀疏：不重扫整个块）。

φ 节点的处理
------------
φ 是“按边”的使用：只有 (前驱, 本块) 边可执行时，该入边值才参与
合流。本实现维护两类驱动：

1. 一条新边到达其目标块时，立即按该入边重新合流块首 φ；
2. φ 的入边值格下降时，若对应边当前可执行，则重新合流该 φ。

首次进入一个块时：先按所有*当前*可执行入边求值 φ，再把非 φ 指令
入队——保证依赖 φ 结果的比较 / 分支在 φ 有值后才被判定。

安全折叠规则（语义保持红线）
---------------------------

1. ``/`` / ``%`` 的除数在格上为常量 0 时，结果给 ``bottom`` 而**不折叠**
   ——该指令运行期必抛错，必须保留到运行期复现；
2. undef 操作数乐观按 ``top`` 合流（SCCP 标准），但优化器永远不会把
   一个引用未初始化变量的使用替换成字面量（见 optimizer 的替换守卫）；
3. ``print`` 是副作用指令：参与 users 链但没有目标、不产生格值，
   优化阶段绝不删除（只允许改写它的操作数）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import BINOPS, UNOPS, CFG, Inst, UNDEF
from .runtime import apply_binop, apply_unop

TOP = "top"
BOTTOM = "bottom"
UNDEF_KIND = "undef"


@dataclass(frozen=True)
class LatticeValue:
    """格元素。kind ∈ {'top','const','undef','bottom'}。"""

    kind: str = TOP
    value: int | None = None

    @staticmethod
    def const(v: int) -> "LatticeValue":
        return LatticeValue("const", v)

    @staticmethod
    def top() -> "LatticeValue":
        return LatticeValue(TOP)

    @staticmethod
    def bottom() -> "LatticeValue":
        return LatticeValue(BOTTOM)

    @staticmethod
    def undef() -> "LatticeValue":
        return LatticeValue(UNDEF_KIND)

    def __str__(self) -> str:
        if self.kind == "const":
            return f"const({self.value})"
        return self.kind

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value}


#: 格高度序（仅用于 meet 的相对排序），数字越大越“确定/低”
_RANK = {TOP: 0, "const": 1, UNDEF_KIND: 2, BOTTOM: 3}


def meet(a: LatticeValue, b: LatticeValue) -> LatticeValue:
    """格合流。

    - top 是单位元（吸收“尚未执行/未初始化入边”的乐观未知）；
    - bottom 吸收一切；
    - undef 是确定的 poison：``undef ∧ const = undef``，
      ``undef ∧ undef = undef``；
    - const∧const：相等保留，否则 bottom。
    """
    if a.kind == TOP:
        return b
    if b.kind == TOP:
        return a
    if a.kind == BOTTOM or b.kind == BOTTOM:
        return LatticeValue.bottom()
    if a.kind == UNDEF_KIND or b.kind == UNDEF_KIND:
        # 至少一边是显式 undef；除非另一边是 bottom（已处理），结果 undef
        return LatticeValue.undef()
    # 两边都是 const
    return a if a.value == b.value else LatticeValue.bottom()


@dataclass
class SCCPResult:
    cfg: CFG
    lattice: dict[str, LatticeValue]
    executable_edges: set[tuple[str, str]]
    reachable: set[str]
    users: dict[str, list[tuple[str, Inst]]] = field(default_factory=dict)

    def value_of(self, name: str) -> LatticeValue:
        return self.lattice.get(name, LatticeValue.top())

    def is_constant(self, name: str) -> int | None:
        lv = self.value_of(name)
        return lv.value if lv.kind == "const" else None

    def is_reachable_block(self, label: str) -> bool:
        return label in self.reachable

    def to_report(self) -> dict:
        return {
            "reachable_blocks": sorted(self.reachable),
            "executable_edges": [list(e) for e in sorted(self.executable_edges)],
            "lattice": {
                name: str(lv)
                for name, lv in sorted(self.lattice.items())
                if lv.kind != TOP
            },
            "constants": {
                name: lv.value
                for name, lv in sorted(self.lattice.items())
                if lv.kind == "const"
            },
        }


class SCCP:
    def __init__(self, cfg: CFG):
        self.cfg = cfg
        # 名字 -> 格值（只登记有定义的 SSA 值；其余按 top 处理）
        self.lattice: dict[str, LatticeValue] = {}
        for _, inst in cfg.instructions():
            if inst.target is not None:
                self.lattice[inst.target] = LatticeValue.top()
        # FIFO 工作流（队列）：CFG 边与 SSA 指令统一处理
        self.flow_wl: list[tuple[str, str]] = []
        self.ssa_wl: list[tuple[str, Inst]] = []
        self.executable: set[tuple[str, str]] = set()
        self.reachable: set[str] = set()
        self._build_users()

    def _build_users(self) -> None:
        """值 -> 使用它的指令（普通指令 + 带边守护的 φ）。"""
        self.users: dict[str, list[tuple[str, Inst]]] = {}
        for block in self.cfg.blocks:
            for inst in block.insts:
                if inst.is_phi:
                    # φ 的每个入边值“在该边可执行时”才是使用者
                    for arg in inst.phi_args:
                        if isinstance(arg.value, str) and arg.value != UNDEF:
                            self.users.setdefault(arg.value, []).append(
                                (block.label, inst))
                else:
                    for o in inst.operands:
                        if isinstance(o, str) and o != UNDEF:
                            self.users.setdefault(o, []).append(
                                (block.label, inst))

    # ---------- 主循环（FIFO） ----------

    def run(self) -> SCCPResult:
        # 虚拟自环 (entry,entry) 标记入口块首次可达
        self._add_edge(self.cfg.entry, self.cfg.entry)
        while self.flow_wl or self.ssa_wl:
            if self.flow_wl:
                src, dst = self.flow_wl.pop(0)
                self._visit_edge(src, dst)
            else:
                block_label, inst = self.ssa_wl.pop(0)
                self._visit_inst(block_label, inst)
        return SCCPResult(self.cfg, dict(self.lattice), self.executable,
                          self.reachable, self.users)

    def _add_edge(self, src: str, dst: str) -> None:
        edge = (src, dst)
        if edge not in self.executable:
            self.executable.add(edge)
            self.flow_wl.append(edge)

    # ---------- CFG 边工作流 ----------

    def _visit_edge(self, src: str, dst: str) -> None:
        block = self.cfg.block(dst)
        first_time = dst not in self.reachable
        if first_time:
            self.reachable.add(dst)

        # 该边（可能是新的前驱）到达：重新合流块首所有 φ。
        # 首次进入时，前驱定义通常已处理（FIFO + 支配序）；即使某入边
        # 值还是 top，后续它下降时会通过 users 链再次唤醒 φ。
        for inst in block.insts:
            if not inst.is_phi:
                break
            self._enqueue_phi(dst, inst)

        if first_time:
            # φ 已入队且排在前面（先压先出），随后才是非 φ 指令
            for inst in block.insts:
                if not inst.is_phi:
                    self.ssa_wl.append((dst, inst))

    def _enqueue_phi(self, block_label: str, inst: Inst) -> None:
        item = (block_label, inst)
        if item not in self.ssa_wl:
            self.ssa_wl.append(item)

    # ---------- SSA 工作流 ----------

    def _visit_inst(self, block_label: str, inst: Inst) -> None:
        if block_label not in self.reachable:
            return
        if inst.is_phi:
            self._eval_phi(block_label, inst)
        elif inst.op == "br":
            self._eval_branch(block_label, inst)
        elif inst.op == "jmp":
            self._add_edge(block_label, inst.blocks[0])
        elif inst.op == "exit":
            return
        elif inst.op == "print":
            # 副作用、无目标：不产生格值，但保留（users 链推动它被重查）
            return
        elif inst.target is not None:
            self._eval_pure(inst)
        else:  # pragma: no cover
            raise AssertionError(f"unexpected inst {inst.op}")

    def _set_and_propagate(self, target: str, lv: LatticeValue) -> None:
        old = self.lattice.get(target, LatticeValue.top())
        new = meet(old, lv)
        if new == old:
            return
        self.lattice[target] = new
        for blk_label, user in self.users.get(target, []):
            if blk_label not in self.reachable:
                continue
            # φ 使用受边守护：只有携带 target 的那条入边当前可执行，
            # target 的下降才会影响该 φ 的合流结果。
            if user.is_phi:
                guard = any(
                    (arg.block, blk_label) in self.executable
                    and arg.value == target
                    for arg in user.phi_args
                )
                if not guard:
                    continue
            item = (blk_label, user)
            if item not in self.ssa_wl:
                self.ssa_wl.append(item)

    # ---- φ ----

    def _eval_phi(self, block_label: str, inst: Inst) -> None:
        assert inst.target is not None
        result = LatticeValue.top()
        for arg in inst.phi_args:
            if (arg.block, block_label) not in self.executable:
                continue  # 未执行的入边不参与合流
            result = meet(result, self._operand_lattice(arg.value))
        self._set_and_propagate(inst.target, result)

    # ---- 纯指令 ----

    def _operand_lattice(self, op) -> LatticeValue:
        if isinstance(op, int):
            return LatticeValue.const(op)
        if op == UNDEF:
            # 显式未定义哨兵：确定的 poison（区别于“边未执行”的 top）
            return LatticeValue.undef()
        return self.lattice.get(op, LatticeValue.undef())

    def _eval_pure(self, inst: Inst) -> None:
        assert inst.target is not None
        self._set_and_propagate(inst.target, self._compute(inst))

    def _compute(self, inst: Inst) -> LatticeValue:
        if inst.op == "lit":
            return LatticeValue.const(int(inst.operands[0]))  # type: ignore[arg-type]
        if inst.op == "copy":
            return self._operand_lattice(inst.operands[0])
        # '-' 既是一元负号也是二元减法：二元分支要求两个操作数，
        # 且必须先于一元分支判定。
        if inst.op in BINOPS and len(inst.operands) == 2:
            la = self._operand_lattice(inst.operands[0])
            lb = self._operand_lattice(inst.operands[1])
            # top = 尚未求值，继续乐观等待
            if la.kind == TOP or lb.kind == TOP:
                return LatticeValue.top()
            # 显式 undef 沿纯运算传播（不触发除零折叠判定）
            if la.kind == UNDEF_KIND or lb.kind == UNDEF_KIND:
                return LatticeValue.undef()
            if la.kind == BOTTOM or lb.kind == BOTTOM:
                return LatticeValue.bottom()
            # 红线：编译期已知除数为 0，运行期必抛错——降级 bottom，
            # 绝不折叠，让该指令保留下来在运行期复现错误。
            if inst.op in ("/", "%") and lb.value == 0:
                return LatticeValue.bottom()
            return LatticeValue.const(apply_binop(inst.op, la.value, lb.value))  # type: ignore[arg-type]
        if inst.op in UNOPS:
            a = self._operand_lattice(inst.operands[0])
            if a.kind == TOP:
                return LatticeValue.top()
            if a.kind == UNDEF_KIND:
                return LatticeValue.undef()
            if a.kind == BOTTOM:
                return LatticeValue.bottom()
            return LatticeValue.const(apply_unop(inst.op, a.value))  # type: ignore[arg-type]
        return LatticeValue.bottom()  # 未知指令：保守

    # ---- 条件分支 ----

    def _eval_branch(self, block_label: str, inst: Inst) -> None:
        op = inst.operands[0]
        # 字面量条件直接判定（不经过格名字表）
        cond_lv = LatticeValue.const(op) if isinstance(op, int) \
            else self._operand_lattice(op)
        then_label, else_label = inst.blocks[0], inst.blocks[1]
        if cond_lv.kind == TOP:
            # 尚未求值：乐观地一条边都不加，等待
            return
        if cond_lv.kind in (BOTTOM, UNDEF_KIND):
            # 运行期条件，或条件是显式 undef（运行期会在该 br 抛
            # undefined-variable）。两种情况下编译期都不能静态选定
            # 单边——保守地把两条边都视为可能执行。
            self._add_edge(block_label, then_label)
            self._add_edge(block_label, else_label)
            return
        # 编译期常量条件：只加被选中的边，另一条分支保持不可达
        if cond_lv.value != 0:
            self._add_edge(block_label, then_label)
        else:
            self._add_edge(block_label, else_label)


def run_sccp(cfg: CFG) -> SCCPResult:
    """对 SSA 形式 CFG 运行 SCCP；只读，不修改 CFG。"""
    return SCCP(cfg).run()
