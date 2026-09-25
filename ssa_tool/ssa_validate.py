"""SSA 合法性校验。

检查项：

1. **单一定义**：每个 SSA 名字在全函数恰好出现一次作为定义（φ/const/算子/param）；
2. **先定义后支配使用**：名字的定义块必须支配每一处使用所在的块（同块内还要求
   定义指令位于使用之前）；
3. **φ 入边完整**：每个 φ 对块的每个前驱恰有一条入边，且入边值在前驱块出口
   处可用（定义块支配前驱）；
4. 不可达块不参与使用校验（其值都是本地 ``const`` / ``u<N>``，已单独保证单定义）。
"""

from __future__ import annotations

from dataclasses import dataclass

from .dominance import analyze
from .errors import SSAError
from .ir import Const, FunctionIR, Name, Operand, PhiInstr


@dataclass
class SSAViolation:
    kind: str
    message: str

    def __str__(self) -> str:
        return f"[{self.kind}] {self.message}"


class SSAValidator:
    def __init__(self, fn: FunctionIR):
        self.fn = fn
        self.dom = analyze(fn)
        self.violations: list[SSAViolation] = []

        self.def_block: dict[str, str] = {}
        self.def_index: dict[str, int] = {}          # 同块内序号（φ 为 -1）

    def fail(self, kind: str, msg: str) -> None:
        self.violations.append(SSAViolation(kind, msg))

    # ---------------- 收集定义 ----------------

    def _collect_defs(self) -> None:
        for b in self.fn.blocks:
            for phi in b.phis:
                self._record_def(phi.dest, b.name, -1, f"φ @{b.name}")
            for idx, ins in enumerate(b.instrs):
                if ins.dest is not None:
                    self._record_def(ins.dest, b.name, idx,
                                     f"{ins.op} @{b.name}")

    def _record_def(self, name: str, block: str, idx: int, where: str) -> None:
        if name in self.def_block:
            self.fail("MULTI_DEF",
                      f"%{name} 在 @{self.def_block[name]} 与 @{block}（{where}）被重复定义")
        else:
            self.def_block[name] = block
            self.def_index[name] = idx

    # ---------------- 使用检查 ----------------

    def _check_use(self, op: Operand, user_block: str, user_idx: int,
                   what: str) -> None:
        if isinstance(op, Const) or not isinstance(op, Name):
            return
        if op.name not in self.def_block:
            self.fail("UNDEF_USE",
                      f"{what} 在 @{user_block} 使用了未定义的 %{op.name}")
            return
        db = self.def_block[op.name]
        if not self.dom.dominates(db, user_block):
            self.fail("USE_NOT_DOMINATED",
                      f"{what}: %{op.name} 定义于 @{db}，不支配使用点 @{user_block}")
            return
        if db == user_block:
            di = self.def_index[op.name]
            if di >= 0 and user_idx >= 0 and di > user_idx:
                self.fail("USE_BEFORE_DEF",
                          f"{what}: %{op.name} 在 @{user_block} 内先使用后定义")

    def _check_block_uses(self) -> None:
        for b in self.fn.blocks:
            if b.name not in self.dom.reachable:
                continue
            for idx, ins in enumerate(b.instrs):
                for k, op in enumerate(ins.operands):
                    self._check_use(op, b.name, idx, f"{ins.op} 的第 {k + 1} 个操作数")
            if b.terminator is not None:
                for k, op in enumerate(b.terminator.operands):
                    self._check_use(op, b.name, 1 << 20,
                                    f"{b.terminator.op} 终结操作数 {k + 1}")

    # ---------------- φ 检查 ----------------

    def _check_phis(self) -> None:
        preds = self.fn.preds()
        for b in self.fn.blocks:
            if b.name not in self.dom.reachable:
                continue
            expected = preds.get(b.name, [])
            for phi in b.phis:
                got = set(phi.incoming)
                want = set(expected)
                if got != want:
                    missing = sorted(want - got)
                    extra = sorted(got - want)
                    self.fail("PHI_INCOMING",
                              f"@{b.name} 的 φ %{phi.dest} 入边不完整，"
                              f"缺 {missing}，多 {extra}")
                for p, v in phi.incoming.items():
                    # φ 的入边值必须在 p 出口可用：定义支配 p
                    if isinstance(v, Name):
                        if v.name not in self.def_block:
                            self.fail("UNDEF_USE",
                                      f"φ %{phi.dest} 来自 @{p} 的值 %{v.name} 未定义")
                            continue
                        db = self.def_block[v.name]
                        if not self.dom.dominates(db, p):
                            self.fail("PHI_VALUE_NOT_AVAILABLE",
                                      f"φ %{phi.dest} 来自 @{p} 的值 %{v.name} "
                                      f"定义于 @{db}，在该边出口不可用")

    # ---------------- 结构检查 ----------------

    def _check_structure(self) -> None:
        if self.fn.flavor != "ssa":
            self.fail("FLAVOR", f"期望 ssa，实际 {self.fn.flavor}")
        for b in self.fn.blocks:
            if b.terminator is None:
                self.fail("NO_TERMINATOR", f"块 @{b.name} 缺少终结指令")
            # SSA 中不允许残留内存指令
            for ins in b.instrs:
                if ins.op in ("alloc", "load", "store"):
                    self.fail("MEM_INSTR",
                              f"SSA 块 @{b.name} 残留内存指令 {ins.op}")

    def validate(self) -> list[SSAViolation]:
        self._check_structure()
        self._collect_defs()
        self._check_block_uses()
        self._check_phis()
        return self.violations


def validate_ssa(fn: FunctionIR, raise_on_error: bool = True) -> list[SSAViolation]:
    violations = SSAValidator(fn).validate()
    if violations and raise_on_error:
        msg = "\n".join(str(v) for v in violations)
        raise SSAError("SSA 校验未通过：\n" + msg)
    return violations
