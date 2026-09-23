"""Independent stack-machine interpreter for closure-converted IR.

The VM consumes only :class:`~sclang.ir.IRModule` (opcodes + constants);
it has **no access to the AST, parser or resolver**. That separation is the
point of the exercise: after conversion, all binding semantics live in the
explicit environment/cell encoding.

Storage model (the converter's contract):

* a call frame owns a flat ``slots`` list and an ``env`` vector holding
  *raw* captured entries — a :class:`Cell` for boxed captures, a plain value
  for immutable captures;
* ``MAKE_CELL`` replaces a slot value with a :class:`Cell`;
* ``MAKE_CLOSURE`` builds the closure's env vector by pulling raw storage
  from the parent frame's local slots or its own env, so a Cell is shared
  by identity all the way down the lexical chain;
* ``CALL`` stashes a pending frame which the run loop pushes before its
  next iteration; ``RET`` pops the current frame and pushes the result.
"""

from dataclasses import dataclass

from .errors import RuntimeError_, Span
from .ir import IRFunction, IRModule, OPCODES


@dataclass
class Cell:
    value: object = None


@dataclass
class VClosure:
    fnid: int
    env: list
    name: str


@dataclass
class Frame:
    func: IRFunction
    slots: list
    env: list
    ip: int = 0


def _span(func: IRFunction, ip: int) -> Span:
    if ip < len(func.span):
        return Span(*func.span[ip])
    return Span(0, 0, 0, 0, 0, 0)


class VM:
    def __init__(self, module: IRModule, capture_output: bool = True):
        self.module = module
        self.capture_output = capture_output
        self.output: list[str] = []
        self.fmap: dict[int, IRFunction] = {f.id: f for f in module.functions}
        self.vstack: list = []
        self.frames: list[Frame] = []
        self.pending: Frame | None = None
        self.result = None

    # -- value helpers ----------------------------------------------------

    @staticmethod
    def format_value(v) -> str:
        if v is None:
            return "nil"
        if v is True:
            return "true"
        if v is False:
            return "false"
        if isinstance(v, VClosure):
            return f"<fn {v.name}>"
        return str(v)

    def _truthy(self, v, span) -> bool:
        if v is None or v is False:
            return False
        if v is True:
            return True
        if isinstance(v, int) and not isinstance(v, bool):
            return v != 0
        raise RuntimeError_(
            f"value {self.format_value(v)!r} cannot be used as a condition",
            span)

    def _int(self, v, span, what="operand"):
        if not isinstance(v, int) or isinstance(v, bool):
            raise RuntimeError_(
                f"{what} must be an integer, got {self.format_value(v)}", span)
        return v

    # -- entry / run loop -------------------------------------------------

    def run(self):
        main = self.fmap.get(0)
        if main is None:  # pragma: no cover - defensive
            raise RuntimeError_("module has no main function (id 0)")
        self.frames.append(Frame(func=main, slots=[None] * main.slots, env=[]))
        while self.frames:
            if self.pending is not None:
                self.frames.append(self.pending)
                self.pending = None
            frame = self.frames[-1]
            if frame.ip >= len(frame.func.code):
                # falling off the end returns nil
                self.frames.pop()
                if self.frames:
                    self.vstack.append(None)
                else:
                    self.result = None
                continue
            ip = frame.ip
            code = frame.func.code
            op = code[ip]
            name = OPCODES[op]
            span = _span(frame.func, ip)
            if name == "CALL":
                # Finish the caller's instruction (advance past CALL) before
                # the callee frame takes over next iteration.
                self._call(code[ip + 1], span)
                frame.ip = ip + 2
                continue
            advance = self._dispatch(name, code, ip, frame, span)
            if not self.frames:
                break  # RET popped the final (main) frame
            if self.pending is None and frame is self.frames[-1]:
                frame.ip = ip + advance
        return self.result

    # -- dispatch ---------------------------------------------------------

    def _dispatch(self, name, code, ip, frame: Frame, span: Span) -> int:
        s = self.vstack

        if name == "CONST":
            s.append(self.module.constants[code[ip + 1]]); return 2
        if name == "NIL":
            s.append(None); return 1
        if name == "TRUE":
            s.append(True); return 1
        if name == "FALSE":
            s.append(False); return 1
        if name == "POP":
            s.pop(); return 1
        if name == "DUP":
            s.append(s[-1]); return 1

        if name == "LOCAL":
            s.append(frame.slots[code[ip + 1]]); return 2
        if name == "SET_LOCAL":
            frame.slots[code[ip + 1]] = s.pop(); return 2
        if name == "MAKE_CELL":
            slot = code[ip + 1]
            frame.slots[slot] = Cell(frame.slots[slot]); return 2
        if name == "LOAD_CELL":
            s.append(frame.slots[code[ip + 1]].value); return 2
        if name == "STORE_CELL":
            frame.slots[code[ip + 1]].value = s.pop(); return 2

        if name == "PUSH_FREE":
            s.append(frame.env[code[ip + 1]]); return 2
        if name == "READ_FREE":
            e = frame.env[code[ip + 1]]
            s.append(e.value if isinstance(e, Cell) else e); return 2
        if name == "WRITE_FREE":
            idx = code[ip + 1]
            entry = frame.env[idx]
            value = s.pop()
            if isinstance(entry, Cell):
                entry.value = value
            else:
                frame.env[idx] = value
            return 2

        if name == "MAKE_CLOSURE":
            return self._make_closure(code, ip, frame)

        # CALL is intercepted by the run loop (it must advance the caller's
        # ip before the callee frame runs), so it never reaches here.

        if name == "RET":
            value = s.pop() if s else None
            self.frames.pop()
            self.result = value
            if self.frames:
                s.append(value)
            return 1

        if name == "JMP":
            return code[ip + 1] - ip
        if name == "JIF_FALSE":
            cond = s.pop()
            if not self._truthy(cond, span):
                return code[ip + 1] - ip
            return 2

        if name == "NOT":
            s.append(not self._truthy(s.pop(), span)); return 1
        if name in ("ADD", "SUB", "MUL", "DIV", "MOD",
                    "EQ", "NE", "LT", "LE", "GT", "GE", "NEG"):
            self._arith(name, span); return 1

        if name == "PRINT":
            argc = code[ip + 1]
            args = s[len(s) - argc:]
            del s[len(s) - argc:]
            text = ", ".join(self.format_value(a) for a in args)
            self.output.append(text)
            if not self.capture_output:
                print(text)
            s.append(None)  # print(...) is a nil-valued expression
            return 2

        raise RuntimeError_(f"unknown opcode {name}", span)  # pragma: no cover

    # -- closures & calls -------------------------------------------------

    def _make_closure(self, code, ip: int, frame: Frame) -> int:
        fnid = code[ip + 1]
        k = code[ip + 2]
        env = []
        for j in range(k):
            kind = code[ip + 3 + 2 * j]
            idx = code[ip + 4 + 2 * j]
            # raw storage: Cell identity is preserved for boxed captures
            env.append(frame.slots[idx] if kind == 0 else frame.env[idx])
        target = self.fmap[fnid]
        self.vstack.append(VClosure(fnid=fnid, env=env, name=target.name))
        return 3 + 2 * k

    def _call(self, argc: int, span: Span):
        s = self.vstack
        callee = s[len(s) - 1 - argc]
        if not isinstance(callee, VClosure):
            raise RuntimeError_(
                f"value {self.format_value(callee)} is not callable", span)
        target = self.fmap[callee.fnid]
        if target.param_count != argc:
            raise RuntimeError_(
                f"function {target.name!r} expects {target.param_count} "
                f"argument(s), got {argc}", span)
        args = s[len(s) - argc:]
        del s[len(s) - 1 - argc:]
        slots = [None] * target.slots
        for i, a in enumerate(args):
            slots[i] = a
        # Pushed on the next run-loop iteration so the caller's current
        # instruction is fully retired first. Boxed parameters are wrapped
        # in Cells by the converter's MAKE_CELL prologue.
        self.pending = Frame(func=target, slots=slots, env=list(callee.env))

    # -- arithmetic -------------------------------------------------------

    def _arith(self, name: str, span: Span):
        s = self.vstack
        if name == "NEG":
            s.append(-self._int(s.pop(), span)); return
        b = s.pop(); a = s.pop()
        if name in ("EQ", "NE"):
            if a is None or b is None:
                eq = a is None and b is None
            elif type(a) is not type(b):
                eq = False  # bool and int are distinct in ScL
            else:
                eq = a == b
            s.append(eq if name == "EQ" else not eq)
            return
        if isinstance(a, VClosure) or isinstance(b, VClosure):
            raise RuntimeError_(f"cannot apply {name} to a function value",
                                span)
        ai = self._int(a, span, "left operand")
        bi = self._int(b, span, "right operand")
        if name in ("LT", "LE", "GT", "GE"):
            s.append({"LT": ai < bi, "LE": ai <= bi,
                      "GT": ai > bi, "GE": ai >= bi}[name]); return
        if name == "ADD":
            s.append(ai + bi)
        elif name == "SUB":
            s.append(ai - bi)
        elif name == "MUL":
            s.append(ai * bi)
        elif name in ("DIV", "MOD"):
            if bi == 0:
                raise RuntimeError_(
                    "integer division by zero" if name == "DIV"
                    else "integer modulo by zero", span)
            q = abs(ai) // abs(bi)
            q = q if (ai < 0) == (bi < 0) else -q
            s.append(q if name == "DIV" else ai - q * bi)


def run_module(module: IRModule):
    """Run an IR module; return ``(result, output_lines)``."""
    vm = VM(module)
    return vm.run(), vm.output
