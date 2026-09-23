"""Tree-free IR interpreter, shared by pre-SSA, SSA and lowered (phi-free) IR.

Execution modes
---------------
* ``mode="memory"`` - variables live in a slot table, accessed through
  ``load`` / ``store``.  Phis and edge arguments are rejected.
* ``mode="ssa"`` - values come from phi nodes and terminator edge
  arguments; loads/stores are rejected.
* ``mode="flat"`` - phi-free executable IR after SSA destruction.  Copies
  are allowed to redefine names (SSA is over); edge arguments are empty.

The returned :class:`InterpResult` contains the return value, captured
``print`` output and the number of executed steps.  Comparing memory-mode
execution of the original IR against flat-mode execution of the lowered IR
is the end-to-end correctness oracle used by the tests.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import VerifyError
from .ir import Module
from .frontend.builder import apply_binop


@dataclass
class InterpResult:
    return_value: int
    output: list[int] = field(default_factory=list)
    steps: int = 0


class StepLimit(Exception):
    pass


class Interpreter:
    def __init__(self, module: Module, mode: str = "memory",
                 step_limit: int = 10_000_000,
                 param_names: dict[str, list[str]] | None = None):
        if mode not in ("memory", "ssa", "flat"):
            raise ValueError(f"unknown interpreter mode {mode!r}")
        self.m = module
        self.mode = mode
        self.step_limit = step_limit
        self.steps = 0
        self.output: list[int] = []
        # In memory mode parameter SSA values also seed the named slots.
        self.param_names = param_names or {}

    def run(self, entry: str = "main", args: list[int] | None = None) -> InterpResult:
        if entry not in self.m.funcs:
            raise VerifyError(f"no such entry function {entry!r}")
        f = self.m.funcs[entry]
        if len(f.params) != len(args or []):
            raise VerifyError(
                f"entry {entry!r} expects {len(f.params)} arguments, "
                f"got {len(args or [])}")
        try:
            ret = self._call(entry, dict(zip(f.params, args or [])),
                             self.param_names.get(entry, []))
        except RecursionError:
            raise VerifyError("call stack exhausted (infinite recursion?)")
        return InterpResult(ret, list(self.output), self.steps)

    def _tick(self):
        self.steps += 1
        if self.steps > self.step_limit:
            raise StepLimit(f"exceeded step limit of {self.step_limit}")

    def _call(self, name: str, params: dict[str, int],
              src_names: list[str] | None = None) -> int:
        f = self.m.funcs[name]
        env: dict[str, int] = dict(params)
        slots: dict[str, int] = {}
        if self.mode == "memory" and src_names:
            for sname, pname in zip(src_names, f.params):
                slots[sname] = params[pname]
        label = f.entry
        branch_args: dict[str, list[int]] = {}

        while True:
            self._tick()
            block = f.blocks[label]

            if self.mode == "ssa":
                incoming = branch_args.pop(label, None)
                if block.phis:
                    if incoming is None or len(incoming) != len(block.phis):
                        raise VerifyError(
                            f"missing phi arguments entering {label!r}")
                    for phi, val in zip(block.phis, incoming):
                        env[phi.dest] = val
            else:
                if block.phis:
                    raise VerifyError(
                        f"phi node remains in block {label!r} in {self.mode} mode")

            for ins in block.instrs:
                self._tick()
                if ins.op == "const":
                    env[ins.dest] = int(ins.attrs["value"])
                elif ins.op == "copy":
                    env[ins.dest] = env[ins.operands[0]]
                elif ins.op == "binop":
                    a = env[ins.operands[0]]
                    b = env[ins.operands[1]]
                    env[ins.dest] = apply_binop(ins.attrs["binop"], a, b)
                elif ins.op == "load":
                    if self.mode != "memory":
                        raise VerifyError("load outside memory mode")
                    # non-parameter slots are zero-initialised at entry;
                    # the frontend forbids textual use before declaration
                    env[ins.dest] = slots.get(ins.attrs["var"], 0)
                elif ins.op == "store":
                    if self.mode != "memory":
                        raise VerifyError("store outside memory mode")
                    slots[ins.attrs["var"]] = env[ins.operands[0]]
                elif ins.op == "call":
                    callee = ins.attrs["name"]
                    if callee not in self.m.funcs:
                        raise VerifyError(f"call to unknown function {callee!r}")
                    cf = self.m.funcs[callee]
                    call_args = [env[o] for o in ins.operands]
                    if len(call_args) != len(cf.params):
                        raise VerifyError(
                            f"function {callee!r} arity mismatch", ins.loc)
                    env[ins.dest] = self._call(
                        callee, dict(zip(cf.params, call_args)),
                        self.param_names.get(callee, []))
                elif ins.op == "print":
                    self.output.append(env[ins.operands[0]])
                else:  # pragma: no cover
                    raise VerifyError(f"cannot execute op {ins.op!r}")

            t = block.term
            if t.kind == "ret":
                if t.operands:
                    return env[t.operands[0]]
                return 0
            if t.kind == "jmp":
                target = t.target
                if self.mode == "ssa" and f.blocks[target].phis:
                    branch_args[target] = [env[a] for a in t.args]
                elif t.args:
                    raise VerifyError("edge arguments outside ssa mode")
                label = target
                continue
            if t.kind == "br":
                cond = env[t.cond]
                if cond != 0:
                    target, args = t.target_t, t.args_t
                else:
                    target, args = t.target_f, t.args_f
                if self.mode == "ssa" and f.blocks[target].phis:
                    branch_args[target] = [env[a] for a in args]
                elif args:
                    raise VerifyError("edge arguments outside ssa mode")
                label = target
                continue
            raise VerifyError(f"bad terminator in {label!r}")  # pragma: no cover


def run_module(m: Module, mode: str, entry="main",
               args: list[int] | None = None) -> InterpResult:
    return Interpreter(m, mode=mode).run(entry, args)
