"""SSA 形式的低级中间表示（IR）与基本块 / CFG。

指令操作数为整数字面量（:class:`int`）、SSA 值名（:class:`str`，形如
``x`` / ``x.1`` / ``$t3``）或未定义哨兵 :data:`UNDEF`。

指令一览::

    lit    target = #n                      整数常量
    copy   target = src                     复制 / 赋值
    binop  target = op(a, b)  op∈+-*/%...   二元运算
    unop   target = op(a)      op∈- !       一元运算
    phi    target = phi(blocks: values...)  φ 节点
    print  print(a)                         副作用，无目标
    br     br cond, then, else              条件分支
    jmp    jmp block                        无条件跳转
    exit    exit                            程序结束

除 ``print`` 外所有指令都是纯的（但 ``/``、``%`` 在除数为运行期 0 时
会抛运行时错误，因此“纯”不等于“可以折叠”——折叠安全性由 SCCP 判断）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .source import Span

#: 未定义值哨兵：读未初始化变量经 SSA 构造得到
UNDEF = "__undef__"

Operand = int | str

# 二元运算
BINOPS = {"+", "-", "*", "/", "%", "==", "!=", "<", ">", "<=", ">="}
# 一元运算
UNOPS = {"-", "!"}
# 可能在除零/模零下抛错的运算
FAULTING_OPS = {"/", "%"}


def is_literal(op: Operand) -> bool:
    return isinstance(op, int)


def is_value(op: Operand) -> bool:
    return isinstance(op, str) and op != UNDEF


def is_undef(op: Operand) -> bool:
    return op == UNDEF


@dataclass
class PhiArg:
    """φ 的一个入边：来自 ``block`` 时取 ``value``。"""

    block: str
    value: Operand


@dataclass
class Inst:
    op: str
    target: str | None = None
    # binop: [a, b]；unop: [a]；copy: [src]；lit: []；print: [a]；br: [cond]
    operands: list[Operand] = field(default_factory=list)
    phi_args: list[PhiArg] = field(default_factory=list)
    # br/jmp 的目标块标签
    blocks: list[str] = field(default_factory=list)
    span: Span | None = None
    # 原始变量名（SSA 重命名后用于调试/可读性）
    debug_name: str | None = None

    # ---- 分类 ----

    @property
    def is_terminator(self) -> bool:
        return self.op in ("br", "jmp", "exit")

    @property
    def has_side_effect(self) -> bool:
        return self.op == "print"

    @property
    def is_phi(self) -> bool:
        return self.op == "phi"

    def copy(self) -> "Inst":
        return Inst(
            self.op, self.target, list(self.operands),
            [PhiArg(a.block, a.value) for a in self.phi_args],
            list(self.blocks), self.span, self.debug_name,
        )


@dataclass
class Block:
    label: str
    insts: list[Inst] = field(default_factory=list)
    preds: list[str] = field(default_factory=list)
    succs: list[str] = field(default_factory=list)

    @property
    def terminator(self) -> Inst | None:
        return self.insts[-1] if self.insts and self.insts[-1].is_terminator else None


@dataclass
class CFG:
    blocks: list[Block] = field(default_factory=list)
    entry: str = "entry"

    # ---- 基本访问 ----

    def block(self, label: str) -> Block:
        for b in self.blocks:
            if b.label == label:
                return b
        raise KeyError(label)

    def has(self, label: str) -> bool:
        return any(b.label == label for b in self.blocks)

    def new_block(self, label: str | None = None) -> Block:
        if label is None:
            label = f"bb{len(self.blocks)}"
        assert not self.has(label), f"duplicate block label {label!r}"
        b = Block(label)
        self.blocks.append(b)
        return b

    def add_edge(self, src: str, dst: str) -> None:
        s, d = self.block(src), self.block(dst)
        if dst not in s.succs:
            s.succs.append(dst)
        if src not in d.preds:
            d.preds.append(src)

    def remove_edge(self, src: str, dst: str) -> None:
        s, d = self.block(src), self.block(dst)
        if dst in s.succs:
            s.succs.remove(dst)
        if src in d.preds:
            d.preds.remove(src)

    def instructions(self):
        for b in self.blocks:
            for i in b.insts:
                yield b, i

    def defined_values(self) -> dict[str, tuple[str, Inst]]:
        """值名 -> (定义块标签, 指令)。SSA 形式下每个名字恰有一个定义。"""
        defs: dict[str, tuple[str, Inst]] = {}
        for b in self.blocks:
            for i in b.insts:
                if i.target is not None:
                    assert i.target not in defs, f"multiple definitions of {i.target!r}"
                    defs[i.target] = (b.label, i)
        return defs

    def clone(self) -> "CFG":
        return deserialize_cfg(serialize_cfg(self))

    # ---- 文本打印（调试 / 可读输出） ----

    def to_text(self) -> str:
        lines: list[str] = []
        for b in self.blocks:
            pred = ", ".join(b.preds) or "-"
            lines.append(f"{b.label}:            ; preds: {pred}")
            for i in b.insts:
                lines.append("  " + format_inst(i))
        return "\n".join(lines)


def format_inst(i: Inst) -> str:
    if i.op == "lit":
        return f"{i.target} = #{int(i.operands[0]) if i.operands else 0}"
    if i.op == "copy":
        return f"{i.target} = copy {fmt_op(i.operands[0])}"
    # 注意：'-' 既是一元负号也是二元减法，按操作数个数区分，
    # 因此二元判断必须在一元之前。
    if i.op in BINOPS and len(i.operands) == 2:
        return f"{i.target} = {i.op} {fmt_op(i.operands[0])}, {fmt_op(i.operands[1])}"
    if i.op in UNOPS:
        return f"{i.target} = {i.op} {fmt_op(i.operands[0])}"
    if i.op == "phi":
        parts = ", ".join(f"[{a.block}: {fmt_op(a.value)}]" for a in i.phi_args)
        return f"{i.target} = phi({parts})"
    if i.op == "print":
        return f"print {fmt_op(i.operands[0])}"
    if i.op == "br":
        return f"br {fmt_op(i.operands[0])}, {i.blocks[0]}, {i.blocks[1]}"
    if i.op == "jmp":
        return f"jmp {i.blocks[0]}"
    if i.op == "exit":
        return "exit"
    raise ValueError(f"unknown op {i.op!r}")


def fmt_op(op: Operand) -> str:
    if isinstance(op, int):
        return f"#{op}"
    return str(op)


# ---------- JSON 序列化 ----------


def _span_dict(span: Span | None):
    return span.to_dict() if span is not None else None


def inst_to_dict(i: Inst) -> dict:
    d: dict = {
        "op": i.op,
        "target": i.target,
        "operands": [("undef" if is_undef(o) else o) for o in i.operands],
        "span": _span_dict(i.span),
        "debug_name": i.debug_name,
    }
    if i.op == "phi":
        d["phi_args"] = [
            {"block": a.block,
             "value": ("undef" if is_undef(a.value) else a.value)}
            for a in i.phi_args
        ]
    if i.blocks:
        d["blocks"] = list(i.blocks)
    return d


def inst_from_dict(d: dict) -> Inst:
    def conv(v):
        return UNDEF if v == "undef" else v

    return Inst(
        d["op"], d.get("target"),
        [conv(o) for o in d.get("operands", [])],
        [PhiArg(a["block"], conv(a["value"])) for a in d.get("phi_args", [])],
        list(d.get("blocks", [])),
        None, d.get("debug_name"),
    )


def serialize_cfg(cfg: CFG) -> dict:
    return {
        "entry": cfg.entry,
        "blocks": [
            {
                "label": b.label,
                "preds": list(b.preds),
                "succs": list(b.succs),
                "insts": [inst_to_dict(i) for i in b.insts],
            }
            for b in cfg.blocks
        ],
    }


def deserialize_cfg(d: dict) -> CFG:
    cfg = CFG(entry=d["entry"])
    for bd in d["blocks"]:
        b = cfg.new_block(bd["label"])
        b.insts = [inst_from_dict(i) for i in bd["insts"]]
    for bd in d["blocks"]:
        for dst in bd["succs"]:
            cfg.add_edge(bd["label"], dst)
    return cfg
