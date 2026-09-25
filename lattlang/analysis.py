"""Sparse Conditional Constant Propagation (Wegman & Zadeck, 1991).

Three-level lattice per SSA value::

        TOP      undefined / not yet determined
         |
    constants (one cell per integer)
         |
       BOTTOM    non-constant

The analysis keeps two interacting work lists:

* **flow work list** — (block, incoming-edge-set): a block's ``phi`` nodes
  are re-evaluated whenever a new incoming edge becomes executable;
* **ssa work list** — values whose lattice cell changed; uses are
  re-evaluated.

Only *executable* edges contribute to phi meet operations, so constants
routed around an edge do not merge with values arriving on unreachable
edges — this is the "conditional" half that lets branches prune code while
the "sparse" half propagates constants directly along SSA def-use edges.

Safety: a constant ``div``/``mod`` with a **zero** divisor is treated as
undefined (TOP) rather than folded, and such instructions are never marked
removable downstream.  A non-constant divisor yields BOTTOM normally; the
instruction is retained so the program's trap behaviour is unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ir import IRProgram, Imm, Instruction, Operand
from .semantics import apply_binary, apply_unary

TOP = "top"
BOTTOM = "bottom"


# --------------------------------------------------------------------------
# Lattice values
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class LatticeValue:
    kind: str                 # "top" | "const" | "bottom"
    value: int | None = None

    @staticmethod
    def top() -> "LatticeValue":
        return LatticeValue(TOP)

    @staticmethod
    def const(v: int) -> "LatticeValue":
        return LatticeValue("const", v)

    @staticmethod
    def bottom() -> "LatticeValue":
        return LatticeValue(BOTTOM)

    def is_top(self) -> bool:
        return self.kind == TOP

    def is_bottom(self) -> bool:
        return self.kind == BOTTOM

    def is_const(self) -> bool:
        return self.kind == "const"

    def to_json(self) -> dict:
        if self.is_const():
            return {"kind": "const", "value": self.value}
        return {"kind": self.kind}


def lattice_meet(a: LatticeValue, b: LatticeValue) -> LatticeValue:
    """Greatest lower bound of the flat lattice."""
    if a.kind == TOP:
        return b
    if b.kind == TOP:
        return a
    if a.kind == BOTTOM or b.kind == BOTTOM:
        return LatticeValue.bottom()
    if a.value == b.value:
        return a
    return LatticeValue.bottom()


# --------------------------------------------------------------------------
# Result
# --------------------------------------------------------------------------

@dataclass
class SCCPResult:
    lat: dict[str, LatticeValue]
    executable_edges: set[tuple[str, str]]
    reachable: set[str]
    # phi results per block (destination name -> value at fixpoint)
    phi_lat: dict[str, dict[str, LatticeValue]] = field(default_factory=dict)

    def value_of(self, op: Operand) -> LatticeValue:
        if isinstance(op, Imm):
            return LatticeValue.const(op.value)
        return self.lat.get(op, LatticeValue.top())

    def is_edge_executable(self, a: str, b: str) -> bool:
        return (a, b) in self.executable_edges


# --------------------------------------------------------------------------
# Analyzer
# --------------------------------------------------------------------------

class SCCPAnalyzer:
    def __init__(self, prog: IRProgram):
        self.prog = prog
        self.lat: dict[str, LatticeValue] = {}
        self.exec_edges: set[tuple[str, str]] = set()
        self.reachable: set[str] = set()
        self.visited: set[str] = set()
        self.flow: list[str] = []
        self.ssa: list[str] = []
        # value-name -> list of (block, instruction-or-'phi dest'-or-term)
        self.users: dict[str, list[tuple[str, object]]] = {}

    # -- bookkeeping -------------------------------------------------------

    def get_lat(self, name: str) -> LatticeValue:
        return self.lat.setdefault(name, LatticeValue.top())

    def set_lat(self, name: str, val: LatticeValue) -> bool:
        old = self.get_lat(name)
        new = lattice_meet(old, val)
        if new != old:
            self.lat[name] = new
            self.ssa.append(name)
            return True
        return False

    def add_edge(self, pred: str, succ: str) -> None:
        if (pred, succ) in self.exec_edges:
            return
        self.exec_edges.add((pred, succ))
        if succ not in self.reachable:
            # First time the block becomes reachable: full first visit
            # evaluates every instruction and the terminator.
            self.reachable.add(succ)
            self.flow.append(succ)
        elif succ in self.visited:
            # Block already had a first visit but gained a newly executable
            # incoming edge: its phis must re-meet over that edge (this is
            # what makes a back edge carrying a new value propagate).
            self.flow.append(succ)

    def value_of(self, op: Operand) -> LatticeValue:
        if isinstance(op, Imm):
            return LatticeValue.const(op.value)
        return self.get_lat(str(op))

    # -- use registration --------------------------------------------------

    def _register_user(self, op: Operand, block_label: str,
                       thing: object) -> None:
        if isinstance(op, str):
            self.users.setdefault(op, []).append((block_label, thing))

    def _build_users(self) -> None:
        for b in self.prog.blocks:
            for dest, args in b.phis.items():
                for a in args:
                    self._register_user(a, b.label, ("phi", dest))
            for ins in b.instrs:
                for a in ins.args:
                    self._register_user(a, b.label, ("instr", ins))
            t = b.term
            if t.cond is not None:
                self._register_user(t.cond, b.label, ("term", t))

    # -- entry point -------------------------------------------------------

    def analyze(self) -> SCCPResult:
        self._build_users()
        entry = self.prog.entry
        self.reachable.add(entry)
        self.flow.append(entry)

        while self.flow or self.ssa:
            while self.flow:
                self.process_flow(self.flow.pop(0))
            if self.ssa:
                self.process_ssa(self.ssa.pop(0))

        phi_lat: dict[str, dict[str, LatticeValue]] = {}
        for b in self.prog.blocks:
            if b.label in self.reachable and b.phis:
                phi_lat[b.label] = {d: self.get_lat(d) for d in b.phis}
        return SCCPResult(self.lat, self.exec_edges, self.reachable, phi_lat)

    # -- flow processing ---------------------------------------------------

    def process_flow(self, label: str) -> None:
        block = self.prog.block(label)
        if label not in self.visited:
            self.visited.add(label)
            # First visit: evaluate every instruction and the terminator.
            for dest in block.phis:
                self.eval_phi(block, dest)
            for ins in block.instrs:
                self.eval_instr(block, ins)
            self.eval_terminator(block)
        else:
            # Only phis can gain information from a newly executable edge.
            for dest in block.phis:
                self.eval_phi(block, dest)

    def eval_phi(self, block, dest: str) -> None:
        args = block.phis[dest]
        acc: LatticeValue | None = None
        found_exec = False
        for i, pred in enumerate(block.preds):
            if (pred, block.label) not in self.exec_edges:
                continue
            found_exec = True
            v = self.value_of(args[i])
            acc = v if acc is None else lattice_meet(acc, v)
        if not found_exec:
            return
        assert acc is not None
        self.set_lat(dest, acc)

    # -- instruction processing -------------------------------------------

    def eval_instr(self, block, ins: Instruction) -> None:
        if ins.kind == "const":
            v = ins.args[0]
            self.set_lat(ins.dest, self.value_of(v))
            return
        if ins.kind == "copy":
            self.set_lat(ins.dest, self.value_of(ins.args[0]))
            return
        if ins.kind == "print":
            return
        # unary / binary
        arg_vals = [self.value_of(a) for a in ins.args]
        result = self.abstract_apply(ins, arg_vals)
        if result is not None:
            self.set_lat(ins.dest, result)

    def abstract_apply(self, ins: Instruction,
                       args: list[LatticeValue]) -> LatticeValue | None:
        if any(v.is_top() for v in args):
            return None
        if any(v.is_bottom() for v in args):
            return LatticeValue.bottom()
        consts = [v.value for v in args]
        if ins.kind == "unary":
            try:
                return LatticeValue.const(apply_unary(ins.op, consts[0]))  # type: ignore[arg-type]
            except (ValueError, ArithmeticError):
                return LatticeValue.bottom()
        # binary
        if ins.op in ("div", "mod") and consts[1] == 0:
            # Definitely traps at runtime.  Do NOT fold: leave the cell TOP
            # so the optimizer can neither replace nor delete the op.
            return None
        try:
            return LatticeValue.const(
                apply_binary(ins.op, consts[0], consts[1]))  # type: ignore[arg-type]
        except (ValueError, ArithmeticError):
            return LatticeValue.bottom()

    def eval_terminator(self, block) -> None:
        t = block.term
        if t.kind == "jmp":
            self.add_edge(block.label, t.targets[0])
        elif t.kind == "br":
            cv = self.value_of(t.cond)
            if cv.is_top():
                return  # direction unknown; no edge yet
            if cv.is_bottom():
                self.add_edge(block.label, t.targets[0])
                self.add_edge(block.label, t.targets[1])
            else:
                target = t.targets[0] if cv.value != 0 else t.targets[1]
                self.add_edge(block.label, target)
        # ret / unreachable: no outgoing edges

    # -- ssa processing ----------------------------------------------------

    def process_ssa(self, name: str) -> None:
        cell = self.get_lat(name)
        for block_label, thing in self.users.get(name, []):
            block = self.prog.block(block_label)
            kind = thing[0]  # type: ignore[index]
            if kind == "phi":
                if block_label in self.reachable:
                    self.eval_phi(block, thing[1])  # type: ignore[index]
            elif kind == "instr":
                ins: Instruction = thing[1]  # type: ignore[index]
                if block_label in self.visited and ins.kind != "print":
                    self.eval_instr(block, ins)
            elif kind == "term":
                if block_label in self.visited:
                    self.eval_terminator(block)


def run_sccp(prog: IRProgram) -> SCCPResult:
    return SCCPAnalyzer(prog).analyze()
