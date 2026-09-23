"""Concrete reference interpreter for Imp.

This is deliberately a *separate* evaluator walking the AST directly (not the
IR used by the abstract analysis): the differential tests compare two
independently written semantics against each other.

Integer semantics:

* arbitrary precision Python ints — arithmetic is over mathematical integers;
* ``/`` and ``%`` are floor division and floor remainder (Python ``//``,``%``);
* division / remainder by zero raises ``div_by_zero``;
* array indices must satisfy ``0 <= i < size``; a negative index is an OOB
  error (there is no Python-style negative indexing).

A step limit protects the JSON service from programs that run forever (the
static analysis handles loops, the concrete interpreter cannot).

Fault-collection mode (:meth:`collect_potential_faults`) does not abort on the
first fault: it records the fault and continues with a safe placeholder. It is
used by the differential tests to check the analyzer's *may*-fault alarms on
operations reachable only after an earlier fault — not by normal execution.
"""

from . import ast_nodes as ast
from .errors import ConcreteExecError

DEFAULT_STEP_LIMIT = 1_000_000


class Exit(Exception):
    pass


class Machine:
    def __init__(self, prog: ast.Program, inputs=None,
                 step_limit=DEFAULT_STEP_LIMIT, probe=False):
        self.prog = prog
        self.steps = 0
        self.step_limit = step_limit
        self.vars = {}
        self.arrays = {}
        self.probe = probe
        self.faults = set()        # (kind, line) collected in probe mode
        self.tainted = False       # a fault already occurred this run
        self.skip_locs = ()        # (kind,line) treated as already past
        inputs = inputs or {}

        for d in prog.var_decls:
            if d.is_input:
                if d.name not in inputs:
                    raise ConcreteExecError(
                        "uninitialized", f"no input supplied for {d.name!r}", d.loc)
                self.vars[d.name] = int(inputs[d.name])
            elif d.init is None:
                # declared but initialized via a preceding assignment (or
                # genuinely uninitialized — reading it later is an error)
                pass
            else:
                self.vars[d.name] = d.init
        for a in prog.arr_decls:
            self.arrays[a.name] = list(a.elems)

    # ------------------------------------------------------------- faults
    def _fault(self, kind, message, loc):
        """In normal execution raise; in probe mode enumerate first faults.

        A fault whose location is in ``skip_locs`` is a previously enumerated
        one: silently continue (with the placeholder value the caller already
        supplied) without tainting.  The first fault at a *new* location is
        recorded and taints the run so masked later faults are ignored.
        """
        line = loc.line if loc is not None else None
        if not self.probe:
            raise ConcreteExecError(kind, message, loc)
        key = (kind, line)
        if key in getattr(self, "skip_locs", ()):
            return
        if not self.tainted:
            self.faults.add(key)
            self.tainted = True

    def check_index(self, i, size, loc):
        if i < 0:
            self._fault("index_out_of_bounds", f"negative array index {i}", loc)
            return False
        if i >= size:
            self._fault(
                "index_out_of_bounds",
                f"array index {i} out of bounds (array size {size})", loc)
            return False
        return True

    def tick(self):
        self.steps += 1
        if self.steps > self.step_limit:
            raise ConcreteExecError(
                "step_limit",
                f"program exceeded the concrete step limit ({self.step_limit})")

    # ------------------------------------------------------------- run
    def run(self):
        self.stmts(self.prog.body)
        return self.snapshot()

    def snapshot(self):
        return {
            "steps": self.steps,
            "scalars": {k: self.vars[k] for k in sorted(self.vars)},
            "arrays": {k: list(self.arrays[k]) for k in sorted(self.arrays)},
        }

    def stmts(self, ss):
        for s in ss:
            self.stmt(s)

    def stmt(self, s):
        self.tick()
        if isinstance(s, ast.Assign):
            v = self.expr(s.value)
            if isinstance(s.target, ast.Var):
                self.vars[s.target.name] = v
            else:
                i = self.expr(s.target.index)
                arr = self.arrays[s.target.name]
                if self.check_index(i, len(arr), s.target.loc):
                    arr[i] = v
                # probe mode: an out-of-bounds store is simply not performed
        elif isinstance(s, ast.If):
            if self.expr(s.cond):
                self.stmts(s.then_body)
            elif s.else_body is not None:
                self.stmts(s.else_body)
        elif isinstance(s, ast.While):
            while self.expr(s.cond):
                self.stmts(s.body)
                self.tick()
        else:
            raise AssertionError(f"bad statement {type(s).__name__}")

    # ------------------------------------------------------------- expr
    def expr(self, e):
        if isinstance(e, ast.IntLit):
            return e.value
        if isinstance(e, ast.BoolLit):
            return e.value
        if isinstance(e, ast.Var):
            if e.name not in self.vars:
                raise ConcreteExecError(
                    "uninitialized", f"variable {e.name!r} not initialized", e.loc)
            return self.vars[e.name]
        if isinstance(e, ast.ArrayRef):
            i = self.expr(e.index)
            arr = self.arrays[e.name]
            if not self.check_index(i, len(arr), e.loc):
                return 0            # placeholder in probe mode
            return arr[i]
        if isinstance(e, ast.Unary):
            v = self.expr(e.expr)
            return -v if e.op == "-" else (not v)
        if isinstance(e, ast.Binary):
            op = e.op
            if op in ("&&", "and"):
                return self.expr(e.lhs) and self.expr(e.rhs)
            if op in ("||", "or"):
                return self.expr(e.lhs) or self.expr(e.rhs)
            a = self.expr(e.lhs)
            b = self.expr(e.rhs)
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                if b == 0:
                    self._fault("div_by_zero", "integer division by zero", e.loc)
                    return 0
                return a // b
            if op == "%":
                if b == 0:
                    self._fault("div_by_zero", "integer remainder by zero", e.loc)
                    return 0
                return a % b
            if op == "==":
                return a == b
            if op == "!=":
                return a != b
            if op == "<":
                return a < b
            if op == "<=":
                return a <= b
            if op == ">":
                return a > b
            if op == ">=":
                return a >= b
        raise AssertionError(f"bad expression {type(e).__name__}")


def execute(prog: ast.Program, inputs=None, step_limit=DEFAULT_STEP_LIMIT):
    return Machine(prog, inputs, step_limit).run()


def collect_potential_faults(prog, inputs, step_limit=DEFAULT_STEP_LIMIT):
    """Run without aborting at the first fault; return set of (kind, line)
    for every faulting operation actually evaluated on these inputs."""
    m = Machine(prog, inputs, step_limit=step_limit, probe=True)
    m.stmts(prog.body)
    return m.faults


def collect_prefix_faults(prog, inputs, step_limit=DEFAULT_STEP_LIMIT,
                          max_rounds=64):
    """Enumerate every fault that can be the FIRST fault of some error-free
    execution prefix for fixed inputs.

    Round 0 runs normally and records the first fault reached. Subsequent
    rounds resume in probe mode while *skipping* (treating as already past)
    all fault locations already enumerated; the first NEW fault encountered is
    added. Repeating until a fixed point yields exactly the operations that
    actually fault on a clean prefix — masking by an earlier fault cannot
    fabricate later ones, and genuinely reachable later faults (e.g. on a
    different branch) are still discovered.
    """
    found = set()
    for _ in range(max_rounds):
        m = Machine(prog, inputs, step_limit=step_limit, probe=True)
        m.skip_locs = set(found)
        m.stmts(prog.body)
        new = m.faults - found
        if not new:
            break
        found |= new
    return found
