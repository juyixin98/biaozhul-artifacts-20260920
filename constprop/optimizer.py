"""基于 SCCP 格结果的保守优化。

只做两类**可证明语义保持**的改写：

1. **常量具体化（RAUW）**——在可达块内
   - 格值为 ``const(n)`` 的 SSA 值，其所有*使用点*（普通操作数、
     ``print`` 参数、``br`` 条件、φ 入边值）直接替换为字面量 ``n``；
     定义指令随后替换为 ``lit #n``（保持“每值唯一定义”的结构不变）。
   - 安全红线一：格值为 const 的定义不可能是“常量除数 0 的 ``/``/``%``”
     ——:class:`~constprop.sccp.SCCP` 对这种情况降级 bottom，故不会
     错误折叠除零。
   - 安全红线二：引用未初始化变量的使用解析为 :data:`UNDEF`，格中
     无其定义，const_of 永远返回 None，故不会被字面量替换（运行期
     undefined-variable 错误行为保持）。
2. **不可达代码删除**
   - 常量条件 ``br`` 改为 ``jmp``，未选中分支整块删除；
   - 从入口沿真实边重算可达集后删除不可达块；
   - 收缩 φ 的入边参数，单活入边 φ 退化为 ``copy``。

``print`` 等副作用指令**绝不单独删除**；只有承载它的块本身不可达时
才随块删除——那时运行期本来就不会执行它。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import CFG, Inst, Operand, PhiArg, UNDEF
from .sccp import SCCPResult, run_sccp


@dataclass
class OptimizationReport:
    sccp: SCCPResult
    constants_before: dict[str, int] = field(default_factory=dict)
    materialized: list[str] = field(default_factory=list)
    simplified_branches: list[dict] = field(default_factory=list)
    removed_blocks: list[str] = field(default_factory=list)
    removed_edges: list[list[str]] = field(default_factory=list)
    phi_pruned: list[str] = field(default_factory=list)
    phi_to_copy: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "sccp": self.sccp.to_report(),
            "constants": self.constants_before,
            "changes": {
                "materialized_constants": self.materialized,
                "simplified_branches": self.simplified_branches,
                "removed_blocks": self.removed_blocks,
                "removed_edges": self.removed_edges,
                "phi_pruned": self.phi_pruned,
                "phi_to_copy": self.phi_to_copy,
            },
        }


def optimize(cfg: CFG) -> tuple[CFG, OptimizationReport]:
    """就地优化 SSA 形式 CFG，返回 (同一 CFG, 报告)。"""
    result = run_sccp(cfg)
    lat = result.lattice
    reachable0 = set(result.reachable)
    consts: dict[str, int] = {
        name: lv.value for name, lv in lat.items() if lv.kind == "const"
    }
    report = OptimizationReport(result, constants_before=dict(sorted(consts.items())))

    def const_of(op: Operand) -> int | None:
        # 字面量本身就是常量；undef 永不具体化
        if isinstance(op, int):
            return op
        if op == UNDEF:
            return None
        return consts.get(op)

    def subst(op: Operand) -> Operand:
        c = const_of(op)
        return c if c is not None else op

    # ---- 阶段 1：可达块内具体化使用点 + br -> jmp ----
    for block in cfg.blocks:
        if block.label not in reachable0:
            continue
        new_insts: list[Inst] = []
        for inst in block.insts:
            if inst.op == "print":
                # 副作用指令保留；只具体化其参数
                op = inst.operands[0]
                c = const_of(op)
                new_insts.append(
                    Inst("print", operands=[c if c is not None else op],
                         span=inst.span)
                )
                continue
            if inst.op == "br":
                c = const_of(inst.operands[0])
                if c is None:
                    cond = subst(inst.operands[0])
                    new_insts.append(Inst("br", operands=[cond],
                                          blocks=list(inst.blocks),
                                          span=inst.span))
                else:
                    chosen = inst.blocks[0] if c != 0 else inst.blocks[1]
                    dropped = inst.blocks[1] if c != 0 else inst.blocks[0]
                    report.simplified_branches.append({
                        "block": block.label, "condition": c,
                        "kept": chosen, "dropped": dropped,
                    })
                    new_insts.append(Inst("jmp", blocks=[chosen],
                                          span=inst.span))
                continue
            if inst.op in ("jmp", "exit"):
                new_insts.append(inst)
                continue

            # 普通指令 / φ：具体化所有操作数与 φ 入边值
            operands = [subst(o) for o in inst.operands]
            phi_args = [PhiArg(a.block, subst(a.value)) for a in inst.phi_args]
            assert inst.target is not None
            c = consts.get(inst.target)
            if c is not None:
                report.materialized.append(inst.target)
                new_insts.append(Inst("lit", target=inst.target,
                                      operands=[c], span=inst.span,
                                      debug_name=inst.debug_name))
            else:
                new_insts.append(Inst(
                    inst.op, target=inst.target, operands=operands,
                    phi_args=phi_args, blocks=list(inst.blocks),
                    span=inst.span, debug_name=inst.debug_name,
                ))
        block.insts = new_insts

    # ---- 阶段 2：摘掉常量分支未选中的 CFG 边 ----
    for block in cfg.blocks:
        if block.label not in reachable0:
            continue
        term = block.terminator
        if term is not None and term.op == "jmp":
            target = term.blocks[0]
            for s in list(block.succs):
                if s != target:
                    report.removed_edges.append([block.label, s])
                    cfg.remove_edge(block.label, s)

    # ---- 阶段 3：沿真实边求最终可达集，删死块，收缩 φ ----
    reachable = _reachable_from_entry(cfg)
    for block in list(cfg.blocks):
        if block.label in reachable:
            continue
        # 先从所有活前驱的邻接表里摘除
        for p in list(block.preds):
            cfg.remove_edge(p, block.label)
        for s in list(block.succs):
            cfg.remove_edge(block.label, s)

    for block in cfg.blocks:
        if block.label not in reachable:
            continue
        live_preds = set(block.preds)
        for idx, inst in enumerate(list(block.insts)):
            if not inst.is_phi:
                break
            kept = [a for a in inst.phi_args if a.block in live_preds]
            if len(kept) != len(inst.phi_args):
                report.phi_pruned.append(inst.target or "?")
            if len(kept) == 1 and inst.target is not None:
                report.phi_to_copy.append(inst.target)
                block.insts[idx] = Inst(
                    "copy", target=inst.target,
                    operands=[kept[0].value], span=inst.span,
                    debug_name=inst.debug_name,
                )
            else:
                inst.phi_args = kept

    dead = sorted(b.label for b in cfg.blocks if b.label not in reachable)
    for label in dead:
        cfg.blocks = [b for b in cfg.blocks if b.label != label]
        report.removed_blocks.append(label)

    # ---- 阶段 4：死代码消除 ----
    # 删除“在活程序中没有任何使用者”的纯指令。这一步必须做：不可达块
    # 删除后，join 块里原本未使用的 φ 可能退化成 copy（入边已随死边
    # 消失，值为 undef）；若不删除，IR 解释器会顺序执行该 copy 并急切
    # 解引用 undef，凭空造出一个运行期未定义错误。
    # print / br / jmp / exit 不是纯定义，永不删除。
    _eliminate_dead_code(cfg)

    return cfg, report


def _reachable_from_entry(cfg: CFG) -> set[str]:
    seen: set[str] = set()
    stack = [cfg.entry]
    while stack:
        label = stack.pop()
        if label in seen:
            continue
        seen.add(label)
        stack.extend(cfg.block(label).succs)
    return seen


def _eliminate_dead_code(cfg: CFG) -> None:
    """删除没有任何使用的*真·纯*定义指令。

    绝不可删除的指令：

    - ``print``（外部可观察副作用）、终结符；
    - ``/`` / ``%``：即使结果无人使用，除数为运行期 0 时仍必须抛
      ``division-by-zero``。删除一条结果未使用的 ``x = 5/0`` 会
      错误地消除运行期错误，违反语义保持。因此它们按“可能陷阱”
      处理，不参与 DCE。

    其余可删除的纯指令：lit/copy/phi、``+ - *`` 与比较、一元运算。
    不动点迭代：删除会释放操作数，可能让更多纯指令死掉。
    """
    removable = (
        {"lit", "copy", "phi", "+", "-", "*",
         "==", "!=", "<", ">", "<=", ">=", "!"}
    )
    for _ in range(10_000):
        used: set[str] = set()
        for block in cfg.blocks:
            for inst in block.insts:
                for o in inst.operands:
                    if isinstance(o, str) and o != UNDEF:
                        used.add(o)
                for a in inst.phi_args:
                    if isinstance(a.value, str) and a.value != UNDEF:
                        used.add(a.value)
        changed = False
        for block in cfg.blocks:
            kept: list[Inst] = []
            for inst in block.insts:
                if (inst.op in removable and inst.target is not None
                        and inst.target not in used):
                    changed = True
                    continue
                kept.append(inst)
            block.insts = kept
        if not changed:
            return
