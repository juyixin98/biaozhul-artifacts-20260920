"""最小 SSA 构造（Cytron 等人的标准算法），不依赖任何编译器框架。

步骤：

1. 求逆后序（RPO）与直接支配者 idom（Cooper–Harvey–Kennedy 迭代算法）；
2. 由 idom 构造支配树与支配边界（DF）；
3. 对每个变量（包括表达式临时变量 ``$t...``）在迭代边界上插入 φ；
4. 沿支配树递归重命名，把每个定值编号为 ``name.k``，每个使用改写为
   当前支配栈顶；从未被赋值就使用的名字解析为 :data:`~constprop.model.UNDEF`
   （运行期即“读未初始化变量”错误）。
"""

from __future__ import annotations

import sys

from .model import CFG, Inst, Operand, PhiArg, UNDEF

sys.setrecursionlimit(1_000_000)


def _reverse_postorder(cfg: CFG) -> list[str]:
    """从 entry 做 DFS，返回逆后序（不可达块排在末尾，按标签序）。"""
    visited: set[str] = set()
    order: list[str] = []

    def dfs(label: str) -> None:
        visited.add(label)
        for s in cfg.block(label).succs:
            if s not in visited:
                dfs(s)
        order.append(label)

    dfs(cfg.entry)
    rpo = list(reversed(order))
    for b in cfg.blocks:
        if b.label not in visited:
            rpo.append(b.label)
    return rpo


def compute_idom(cfg: CFG) -> dict[str, str | None]:
    """Cooper–Harvey–Kennedy 迭代求直接支配者。"""
    rpo = _reverse_postorder(cfg)
    rpo_index = {label: i for i, label in enumerate(rpo)}
    idom: dict[str, str | None] = {b.label: None for b in cfg.blocks}
    if not rpo:
        return idom
    entry = rpo[0]
    idom[entry] = entry

    def intersect(a: str, b: str) -> str:
        fa, fb = a, b
        while fa != fb:
            while rpo_index[fa] > rpo_index[fb]:
                fa = idom[fa]  # type: ignore[assignment]
            while rpo_index[fb] > rpo_index[fa]:
                fb = idom[fb]  # type: ignore[assignment]
        return fa

    changed = True
    while changed:
        changed = False
        for label in rpo[1:]:
            b = cfg.block(label)
            processed = [p for p in b.preds if idom[p] is not None]
            if not processed:
                continue
            new_idom = processed[0]
            for p in processed[1:]:
                new_idom = intersect(p, new_idom)
            if idom[label] != new_idom:
                idom[label] = new_idom
                changed = True
    return idom


def dominator_tree(cfg: CFG, idom: dict[str, str | None]) -> dict[str, list[str]]:
    children: dict[str, list[str]] = {b.label: [] for b in cfg.blocks}
    for b in cfg.blocks:
        d = idom[b.label]
        if d is not None and d != b.label:
            children[d].append(b.label)
    for lst in children.values():
        lst.sort(key=lambda l: cfg.block(l).label)
    return children


def dominance_frontiers(cfg: CFG, idom: dict[str, str | None]) -> dict[str, set[str]]:
    df: dict[str, set[str]] = {b.label: set() for b in cfg.blocks}
    for b in cfg.blocks:
        if len(b.preds) < 2:
            continue
        b_idom = idom[b.label]
        for p in b.preds:
            runner: str | None = p
            while runner is not None and runner != b_idom:
                df[runner].add(b.label)
                d = idom[runner]
                runner = d if d != runner else None
    return df


def _definition_sites(cfg: CFG) -> dict[str, list[str]]:
    """需要参与 φ 插入的名字 -> 在其中被定值的基本块。

    - 源变量（赋值目标）：可在多个分支/循环迭代中定值，需要 φ；
    - 短路逻辑结果临时值（``$j...``）：在 short / eval 两条路径上各
      定值一次、join 后使用，是跨支配边界的合流，需要 φ；
    - 普通表达式临时值（``$t...``）：在表达式出现点单次定值且支配其
      使用，**不**插 φ（否则会给循环头条件临时值插入错误的循环 φ）。
    """
    sites: dict[str, list[str]] = {}
    for b in cfg.blocks:
        for i in b.insts:
            if i.target is None:
                continue
            name = i.debug_name or i.target
            if name.startswith("$t") and not name.startswith("$j"):
                continue
            if b.label not in sites.setdefault(name, []):
                sites[name].append(b.label)
    return sites


def insert_phi_functions(cfg: CFG, df: dict[str, set[str]]) -> None:
    """在迭代支配边界上为每个变量插入 φ（每块每变量至多一个）。"""
    sites = _definition_sites(cfg)
    # 变量按首次定值顺序，保证输出确定
    var_order = list(sites.keys())
    for name in var_order:
        worklist = list(sites[name])
        seen: set[str] = set()
        phi_blocks: set[str] = set()
        while worklist:
            x = worklist.pop()
            for d in sorted(df[x]):
                if d in phi_blocks:
                    continue
                block = cfg.block(d)
                phi = Inst(
                    "phi", target=name,
                    phi_args=[PhiArg(p, 0) for p in block.preds],
                    debug_name=name,
                )
                # 插到所有 φ 区域（块首）
                idx = 0
                while idx < len(block.insts) and block.insts[idx].is_phi:
                    idx += 1
                block.insts.insert(idx, phi)
                phi_blocks.add(d)
                if d not in seen:
                    seen.add(d)
                    if d not in sites[name]:
                        worklist.append(d)


def rename_to_ssa(cfg: CFG, idom: dict[str, str | None]) -> None:
    """沿支配树重命名：定值加版本号，使用绑定到栈顶，缺失则 UNDEF。"""
    children = dominator_tree(cfg, idom)
    stacks: dict[str, list[str]] = {}
    counter: dict[str, int] = {}

    def lookup(name: str) -> Operand:
        st = stacks.get(name)
        if st:
            return st[-1]
        return UNDEF

    def new_name(base: str) -> str:
        counter[base] = counter.get(base, 0) + 1
        versioned = f"{base}.{counter[base]}"
        stacks.setdefault(base, []).append(versioned)
        return versioned

    def rename_operand(o: Operand) -> Operand:
        if isinstance(o, int) or o == UNDEF:
            return o
        return lookup(o)

    def rename_block(label: str) -> None:
        block = cfg.block(label)
        pushed: list[str] = []

        # 1) φ 的定值先编号
        for inst in block.insts:
            if not inst.is_phi:
                continue
            base = inst.debug_name or inst.target
            assert inst.target is not None and base is not None
            inst.target = new_name(base)
            pushed.append(base)

        # 2) 普通指令：改写使用，再给定值编号
        for inst in block.insts:
            if inst.is_phi:
                continue
            inst.operands = [rename_operand(o) for o in inst.operands]
            if inst.target is not None:
                base = inst.debug_name or inst.target
                inst.target = new_name(base)
                pushed.append(base)

        # 3) 给后继块的 φ 入边填值（取本块末尾的到达定值）
        for succ_label in block.succs:
            succ = cfg.block(succ_label)
            for inst in succ.insts:
                if not inst.is_phi:
                    break
                base = inst.debug_name
                assert base is not None
                for arg in inst.phi_args:
                    if arg.block == label:
                        arg.value = lookup(base)

        # 4) 递归支配子树
        for child in children[label]:
            rename_block(child)

        # 5) 恢复栈
        for base in pushed:
            stacks[base].pop()

    rename_block(cfg.entry)

    # 校验：所有 φ 入边都被某个前驱填过（结构化程序必然如此）
    for b in cfg.blocks:
        for inst in b.insts:
            if inst.is_phi:
                preds = set(b.preds)
                filled = {a.block for a in inst.phi_args}
                assert filled == preds, f"phi in {b.label} args {filled} != preds {preds}"


def construct_ssa(cfg: CFG) -> CFG:
    """就地把可化简 CFG 转成最小 SSA，返回同一对象。"""
    idom = compute_idom(cfg)
    df = dominance_frontiers(cfg, idom)
    insert_phi_functions(cfg, df)
    rename_to_ssa(cfg, idom)
    return cfg
