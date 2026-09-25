"""整数 IR 的统一数据结构。

同一个 :class:`FunctionIR` 贯穿三个阶段，仅允许出现的指令不同：

1. **原始 IR (raw)**：内存风格，含 ``alloc / load / store``，无 φ。
2. **SSA IR**：所有值单一定义，块首可有 φ 指令，无 ``alloc/load/store``。
3. **可执行 IR (exec)**：φ 已被消除为边上的并行复制（已串行化），无 φ。

名字约定（文本形式）：

- ``%x``      原始 IR 的内存槽（alloc 目标 / load、store 访问）
- ``%t0``     原始 IR 临时寄存器
- ``%x.0``    SSA 名字（槽 x 的第 0 代；临时寄存器沿用 ``%tN``）
- ``@label``  基本块标号
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .ast_nodes import Span


# ===================== 操作数 =====================

@dataclass(frozen=True)
class Const:
    """立即数。"""
    value: int

    @property
    def text(self) -> str:
        return str(self.value)

    def as_dict(self) -> dict:
        return {"kind": "const", "value": self.value}


@dataclass(frozen=True)
class Name:
    """寄存器 / SSA 值引用（不带 '%'）。"""
    name: str

    @property
    def text(self) -> str:
        return f"%{self.name}"

    def as_dict(self) -> dict:
        return {"kind": "name", "value": self.name}


Operand = Const | Name


def operand_text(op: Operand) -> str:
    return op.text


# ===================== 指令 =====================

# 终结指令
TERMINATORS = {"jmp", "br", "ret", "unreachable"}

# 纯值算子（arity -> 结果）
BINARY_OPS = {"add", "sub", "mul", "div", "mod",
              "lt", "le", "gt", "ge", "eq", "ne"}
UNARY_OPS = {"neg", "lnot"}


@dataclass
class PhiInstr:
    """φ 指令：``%dest = phi [@pred: 值, ...]``，属于某个内存槽 slot。"""
    dest: str
    slot: str                      # 不带 '%' 的槽名
    incoming: dict[str, Operand] = field(default_factory=dict)
    span: Span | None = None

    def copy(self) -> "PhiInstr":
        return PhiInstr(self.dest, self.slot, dict(self.incoming), self.span)

    def as_dict(self) -> dict:
        return {
            "dest": self.dest,
            "slot": self.slot,
            "incoming": [{"pred": p, "value": v.as_dict()}
                         for p, v in self.incoming.items()],
            "span": self.span.as_dict() if self.span else None,
        }


@dataclass
class Instr:
    """一条普通指令或终结指令。

    - 二元/一元算术：``%dest = add %a, %b``
    - const/param：``%dest = const 7`` / ``%dest = param 0``
    - load/store：``%dest = load %slot`` / ``store %值 -> %slot``（slot 单独存放）
    - jmp/br：``jmp @b`` / ``br %cond, @t, @f``（目标在 blocks）
    - ret：``ret %值`` 或 ``ret``
    - copy（exec 阶段）：``%dest = copy %a``
    """
    op: str
    dest: str | None = None
    operands: list[Operand] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)
    slot: str | None = None
    span: Span | None = None

    def copy(self) -> "Instr":
        return Instr(self.op, self.dest, list(self.operands),
                     list(self.blocks), self.slot, self.span)

    def as_dict(self) -> dict:
        return {
            "op": self.op,
            "dest": self.dest,
            "operands": [o.as_dict() for o in self.operands],
            "blocks": list(self.blocks),
            "slot": self.slot,
            "span": self.span.as_dict() if self.span else None,
        }


# ===================== 基本块 / 函数 =====================

@dataclass
class Block:
    name: str
    phis: list[PhiInstr] = field(default_factory=list)
    instrs: list[Instr] = field(default_factory=list)
    terminator: Instr | None = None

    def copy(self) -> "Block":
        return Block(self.name,
                     [p.copy() for p in self.phis],
                     [i.copy() for i in self.instrs],
                     self.terminator.copy() if self.terminator else None)

    def is_terminated(self) -> bool:
        return self.terminator is not None

    def successors(self) -> list[str]:
        if self.terminator is None:
            return []
        return list(self.terminator.blocks)

    def as_dict(self) -> dict:
        return {
            "label": self.name,
            "phis": [p.as_dict() for p in self.phis],
            "instrs": [i.as_dict() for i in self.instrs],
            "terminator": self.terminator.as_dict() if self.terminator else None,
        }


@dataclass
class FunctionIR:
    name: str
    params: list[str]
    blocks: list[Block]
    flavor: str = "raw"            # raw | ssa | exec

    # ---- 便捷查询 ----
    def block(self, name: str) -> Block:
        for b in self.blocks:
            if b.name == name:
                return b
        raise KeyError(name)

    def has_block(self, name: str) -> bool:
        return any(b.name == name for b in self.blocks)

    @property
    def entry(self) -> Block:
        return self.blocks[0]

    def preds(self) -> dict[str, list[str]]:
        """按块在终结指令中出现的顺序返回前驱表。"""
        out: dict[str, list[str]] = {b.name: [] for b in self.blocks}
        for b in self.blocks:
            for s in b.successors():
                if s in out:
                    out[s].append(b.name)
        return out

    def copy(self) -> "FunctionIR":
        return FunctionIR(self.name, list(self.params),
                          [b.copy() for b in self.blocks], self.flavor)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "params": list(self.params),
            "flavor": self.flavor,
            "blocks": [b.as_dict() for b in self.blocks],
        }


# ===================== 文本打印 =====================

def _fmt_terminator(i: Instr) -> str:
    if i.op == "jmp":
        return f"jmp @{i.blocks[0]}"
    if i.op == "br":
        return f"br {i.operands[0].text}, @{i.blocks[0]}, @{i.blocks[1]}"
    if i.op == "ret":
        if i.operands:
            return f"ret {i.operands[0].text}"
        return "ret"
    if i.op == "unreachable":
        return "unreachable"
    raise ValueError(f"未知终结指令 {i.op}")


def _fmt_instr(i: Instr) -> str:
    if i.op in TERMINATORS:
        return _fmt_terminator(i)
    prefix = f"{Name(i.dest).text} = " if i.dest is not None else ""
    if i.op == "alloc":
        init = i.operands[0].text if i.operands else "0"
        body = f"alloc {init}"
    elif i.op == "load":
        body = f"load {Name(i.slot).text}"
    elif i.op == "store":
        body = f"store {i.operands[0].text} -> {Name(i.slot).text}"
    elif i.op == "const":
        body = f"const {i.operands[0].text}"
    elif i.op == "param":
        body = f"param {i.operands[0].text}"
    elif i.op == "copy":
        body = f"copy {i.operands[0].text}"
    elif i.op in BINARY_OPS:
        body = f"{i.op} {i.operands[0].text}, {i.operands[1].text}"
    elif i.op in UNARY_OPS:
        body = f"{i.op} {i.operands[0].text}"
    else:
        body = f"{i.op} " + ", ".join(o.text for o in i.operands)
    return prefix + body


def dump_function(fn: FunctionIR) -> str:
    """渲染为人类可读的 IR 文本。"""
    lines: list[str] = []
    param_decl = ", ".join(f"%{p}" for p in fn.params)
    lines.append(f"func {fn.name}({param_decl}) // flavor: {fn.flavor}")
    lines.append("{")
    pred_map = fn.preds()
    for b in fn.blocks:
        preds = ", ".join(f"@{p}" for p in pred_map[b.name]) or "-"
        lines.append(f"@{b.name}:            ; preds: {preds}")
        for phi in b.phis:
            inc = ", ".join(f"[@%s: %s]" % (p, v.text)
                            for p, v in phi.incoming.items())
            lines.append(f"  {Name(phi.dest).text} = phi {inc}    ; slot %{phi.slot}")
        for ins in b.instrs:
            lines.append("  " + _fmt_instr(ins))
        if b.terminator is not None:
            lines.append("  " + _fmt_instr(b.terminator))
    lines.append("}")
    return "\n".join(lines)
