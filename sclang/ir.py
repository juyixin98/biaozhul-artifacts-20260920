"""Closure-conversion intermediate representation (CCIR).

The conversion rewrites lexical nesting into flat function records with
explicit **environment** handling:

* every source function (the program becomes ``main``) is an
  :class:`IRFunction` with a local frame of numbered slots and a list
  ``free`` naming the bindings captured from enclosing functions;
* a captured binding that the resolver proved is mutated is carried as a
  shared :class:`Cell` (``MAKE_CELL`` / ``LOAD_CELL`` / ``STORE_CELL``);
  immutable captures are copied by value;
* nested-function creation becomes ``MAKE_CLOSURE`` whose operand is an
  environment vector assembled slot-by-slot from the current frame's locals
  or its own free environment.

The representation is plain data (lists of ints + a constant table), so it
round-trips through JSON for the web service.
"""

from dataclasses import dataclass, field


# -- opcodes (operands documented inline) ----------------------------------
#
# Stack discipline: operands are pushed; binary ops pop two and push one.
#
# CONST idx            push constants[idx]
# NIL / TRUE / FALSE   push literal
# POP                  discard top
# DUP                  duplicate top
#
# LOCAL slot           push local slot (plain value)
# SET_LOCAL slot      pop value into local slot
#
# MAKE_CELL slot       local slot := new Cell(value already in slot) ... see note
# LOAD_CELL slot       push cell[slot].value          (cell in local frame)
# STORE_CELL slot      pop -> cell[slot].value
#
# PUSH_FREE idx        push current env entry idx (raw Cell or value)
# READ_FREE idx        push env[idx], dereferencing a Cell
# WRITE_FREE idx       pop -> write env[idx] (through Cell if boxed)
#
# MAKE_CLOSURE fnid k  build closure of functions[fnid]; next k*2 operands
#                      on the emitted instruction list describe env entries:
#                      pairs (source_kind, source_index) where
#                        (0, slot)  read local slot (raw, keeps Cell identity)
#                        (1, idx)   read own free entry idx (raw)
# CALL argc            call top-of-stack function with argc args
# RET                  return top of stack (or nil if stack empty)
#
# JMP target / JIF_FALSE target   (pops condition on JIF_FALSE)
#
# ADD SUB MUL DIV MOD
# EQ NE LT LE GT GE
# NEG NOT
# AND / OR are short-circuited at compile time via jumps (no opcode).
#
# PRINT argc           format+append argc popped values to program output
#
# NOTE on MAKE_CELL: to keep one opcode per source operation we emit
#   LOCAL slot          (or the initializer result is already there)
#   MAKE_CELL slot      wrap slots[slot] in a Cell in place

OPCODES = [
    "CONST", "NIL", "TRUE", "FALSE", "POP", "DUP",
    "LOCAL", "SET_LOCAL",
    "MAKE_CELL", "LOAD_CELL", "STORE_CELL",
    "PUSH_FREE", "READ_FREE", "WRITE_FREE",
    "MAKE_CLOSURE", "CALL", "RET",
    "JMP", "JIF_FALSE",
    "ADD", "SUB", "MUL", "DIV", "MOD",
    "EQ", "NE", "LT", "LE", "GT", "GE",
    "NEG", "NOT",
    "PRINT",
]
OP = {name: i for i, name in enumerate(OPCODES)}


@dataclass
class IRFunction:
    id: int
    name: str
    param_count: int
    slots: int
    # each item: {"name": str, "boxed": bool}
    free: list[dict] = field(default_factory=list)
    # each item: {"name": str, "span": [start,end,line,col,eline,ecol]}
    params: list[dict] = field(default_factory=list)
    code: list[int] = field(default_factory=list)
    span: list[list[int]] = field(default_factory=list)  # per-instruction span

    def emit(self, opcode: int, *operands: int, span=None):
        self.code.append(opcode)
        self.span.append(_span6(span))
        for op in operands:
            self.code.append(op)
            self.span.append(_span6(span))

    def here(self) -> int:
        return len(self.code)


def _span6(span) -> list[int]:
    if span is None:
        return [0, 0, 0, 0, 0, 0]
    return [span.start, span.end, span.line, span.col,
            span.end_line, span.end_col]


@dataclass
class IRModule:
    functions: list[IRFunction] = field(default_factory=list)
    constants: list = field(default_factory=list)  # int | str

    def add_const(self, value) -> int:
        # dedupe by value/type to keep the table small
        for i, c in enumerate(self.constants):
            if type(c) is type(value) and c == value:
                return i
        self.constants.append(value)
        return len(self.constants) - 1

    def to_dict(self) -> dict:
        return {
            "constants": self.constants,
            "functions": [
                {
                    "id": f.id,
                    "name": f.name,
                    "param_count": f.param_count,
                    "slots": f.slots,
                    "params": f.params,
                    "free": f.free,
                    "code": f.code,
                    "spans": f.span,
                }
                for f in self.functions
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> "IRModule":
        mod = cls(constants=list(data.get("constants", [])))
        for fd in data["functions"]:
            f = IRFunction(
                id=fd["id"], name=fd["name"],
                param_count=fd["param_count"], slots=fd["slots"],
                free=list(fd.get("free", [])),
                params=list(fd.get("params", [])),
                code=list(fd["code"]),
                span=list(fd.get("spans", [])))
            mod.functions.append(f)
        return mod


def disassemble(mod: IRModule) -> str:
    """Human-readable listing (used by CLI ``dump`` and tests)."""
    lines = []
    for f in mod.functions:
        frees = ", ".join(
            f"{e['name']}{'*' if e['boxed'] else ''}" for e in f.free)
        lines.append(
            f"fn {f.id} {f.name}({f.param_count}) slots={f.slots} "
            f"free=[{frees}]")
        i = 0
        while i < len(f.code):
            op = f.code[i]
            name = OPCODES[op]
            args = ""
            width = 1
            if name in ("CONST", "LOCAL", "SET_LOCAL", "MAKE_CELL",
                        "LOAD_CELL", "STORE_CELL", "PUSH_FREE", "READ_FREE",
                        "WRITE_FREE", "JMP", "JIF_FALSE", "PRINT", "CALL"):
                args = f" {f.code[i + 1]}"
                width = 2
            if name == "MAKE_CLOSURE":
                fnid = f.code[i + 1]
                k = f.code[i + 2]
                rest = [f.code[i + 3 + 2 * j: i + 5 + 2 * j]
                        for j in range(k)]
                args = f" fn={fnid} k={k} env={rest}"
                width = 3 + 2 * k
            sp = f.span[i] if i < len(f.span) else [0] * 6
            loc = f"{sp[2]}:{sp[3]}" if sp[2] else "  -  "
            lines.append(f"  {i:4d} {loc:>8} {name:<12}{args}")
            i += width
    return "\n".join(lines)
