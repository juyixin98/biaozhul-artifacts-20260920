"""栈式字节码定义与二进制模块编解码。

二进制模块格式 (v1, 小端序)::

    magic   4B   b"BV\\x01\\x00"
    count   1B   函数数量 (1..255)
    funcs[] 每个函数:
        name_len    1B, name     UTF-8
        ret_tag     1B   (TAG_*)
        nparams     1B
        params[]    nparams * 1B 参数类型标签
        nlocals     1B
        locals[]    nlocals * 1B 局部变量声明类型标签
        max_stack   1B
        code_len    2B
        code        code_len 字节
        const_len   2B
        consts[]    常量池: 1B 标签 + 值
                    int  -> 8B 有符号小端
                    bool -> 1B (0/1)

操作数宽度固定：所有跳转/索引立即数均为 2B 无符号小端。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .common import PHASE_DECODE, Span, ToolError

MAGIC = b"BV\x01\x00"

# ---------- 操作码 ----------

OP_LOAD_CONST = 0x10
OP_LOAD_LOCAL = 0x11
OP_STORE_LOCAL = 0x12
OP_POP = 0x13

OP_ADD = 0x20
OP_SUB = 0x21
OP_MUL = 0x22
OP_DIV = 0x23
OP_NEG = 0x24
OP_NOT = 0x25

OP_EQ = 0x30
OP_NE = 0x31
OP_LT = 0x32
OP_GT = 0x33
OP_LE = 0x34
OP_GE = 0x35

OP_JMP = 0x40
OP_JIF = 0x41

OP_CALL = 0x50
OP_RETV = 0x51
OP_RET = 0x52

# 助记符表（同时用于反汇编）
OPCODE_NAMES = {
    OP_LOAD_CONST: "LOAD_CONST",
    OP_LOAD_LOCAL: "LOAD_LOCAL",
    OP_STORE_LOCAL: "STORE_LOCAL",
    OP_POP: "POP",
    OP_ADD: "ADD", OP_SUB: "SUB", OP_MUL: "MUL", OP_DIV: "DIV",
    OP_NEG: "NEG", OP_NOT: "NOT",
    OP_EQ: "EQ", OP_NE: "NE", OP_LT: "LT", OP_GT: "GT",
    OP_LE: "LE", OP_GE: "GE",
    OP_JMP: "JMP", OP_JIF: "JIF",
    OP_CALL: "CALL", OP_RETV: "RETV", OP_RET: "RET",
}
NAME_TO_OPCODE = {v: k for k, v in OPCODE_NAMES.items()}

# 操作数宽度（字节）。0 表示无操作数
OPERAND_SIZE = {
    OP_LOAD_CONST: 2,
    OP_LOAD_LOCAL: 2,
    OP_STORE_LOCAL: 2,
    OP_POP: 0,
    OP_ADD: 0, OP_SUB: 0, OP_MUL: 0, OP_DIV: 0,
    OP_NEG: 0, OP_NOT: 0,
    OP_EQ: 0, OP_NE: 0, OP_LT: 0, OP_GT: 0, OP_LE: 0, OP_GE: 0,
    OP_JMP: 2, OP_JIF: 2,
    OP_CALL: 2, OP_RETV: 0, OP_RET: 0,
}
# 哪些指令带跳转目标
JUMP_OPS = {OP_JMP, OP_JIF}

# ---------- 类型标签（验证器使用的抽象值类型） ----------

TAG_INT = 1
TAG_BOOL = 2
TAG_VOID = 3

TAG_NAMES = {TAG_INT: "int", TAG_BOOL: "bool", TAG_VOID: "void"}
TYPE_NAME_TO_TAG = {"int": TAG_INT, "bool": TAG_BOOL, "void": TAG_VOID}

# 内建函数占用高编号（用户函数下标从 0 开始，永远不会冲突）
BUILTIN_PRINT_INT = 0xFFF0
BUILTIN_PRINT_BOOL = 0xFFF1
BUILTIN_RANGE = range(BUILTIN_PRINT_INT, BUILTIN_PRINT_BOOL + 1)


@dataclass
class Instruction:
    opcode: int
    operand: int = 0
    pc: int = 0
    # 编译期调试信息（不写入二进制）：指令对应的源码位置
    span: Span | None = None
    # CALL 的实参个数（不写入二进制；编译期元数据，运行/验证时按函数签名取）
    arg_count: int = 0


@dataclass
class CodeObject:
    name: str
    ret_tag: int
    param_tags: list[int]
    local_tags: list[int]                 # 局部变量“声明类型”（槽位在参数之后）
    instructions: list[Instruction] = field(default_factory=list)
    consts: list[tuple[int, int | bool]] = field(default_factory=list)
    max_stack: int = 0
    declared_code_len: int = 0          # 头部声明的代码段长度（解码时填充）
    # 槽位名（仅调试/错误信息，不写入二进制）
    slot_names: list[str] = field(default_factory=list)
    # 指令 pc -> 源码行，方便错误渲染（不写入二进制）
    pc_spans: dict = field(default_factory=dict)
    # 源码行文本与文件名（不写入二进制；变异/解码得到的模块为 None）
    source_lines: list[str] | None = None
    filename: str = ""

    @property
    def nslots(self) -> int:
        return len(self.param_tags) + len(self.local_tags)

    def encode(self) -> bytes:
        code = self._encode_code()
        out = bytearray()
        out.append(len(self.name.encode("utf-8")))
        out += self.name.encode("utf-8")
        out.append(self.ret_tag)
        out.append(len(self.param_tags))
        out += bytes(self.param_tags)
        out.append(len(self.local_tags))
        out += bytes(self.local_tags)
        out.append(self.max_stack)
        out += len(code).to_bytes(2, "little")
        out += code
        out += len(self.consts).to_bytes(2, "little")
        for tag, val in self.consts:
            out.append(tag)
            if tag == TAG_INT:
                out += int(val).to_bytes(8, "little", signed=True)
            elif tag == TAG_BOOL:
                out.append(1 if val else 0)
            else:
                raise ToolError(
                    PHASE_DECODE, "const.tag",
                    f"常量池出现非法类型标签 {tag}",
                )
        return bytes(out)

    def _encode_code(self) -> bytes:
        # 约定：内存中所有 2B 操作数（含跳转目标）都是最终值。
        # 跳转目标由编译器在布局后写成字节偏移，这里原样输出。
        raw = bytearray()
        for ins in self.instructions:
            raw.append(ins.opcode)
            if OPERAND_SIZE[ins.opcode] == 2:
                raw += ins.operand.to_bytes(2, "little")
        return bytes(raw)


@dataclass
class Module:
    functions: list[CodeObject]

    def by_name(self, name: str) -> CodeObject | None:
        for f in self.functions:
            if f.name == name:
                return f
        return None

    def encode(self) -> bytes:
        if not (1 <= len(self.functions) <= 255):
            raise ToolError(
                PHASE_DECODE, "module.funcs",
                f"函数数量必须在 1..255，实际 {len(self.functions)}",
            )
        out = bytearray(MAGIC)
        out.append(len(self.functions))
        for f in self.functions:
            out += f.encode()
        return bytes(out)


# ---------- 解码 ----------

class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.i = 0

    def take(self, n: int, what: str) -> bytes:
        if self.i + n > len(self.d):
            raise ToolError(
                PHASE_DECODE, "truncated",
                f"模块被截断：读取 {what} 时超出文件末尾 (offset={self.i})",
            )
        b = self.d[self.i:self.i + n]
        self.i += n
        return b

    def u8(self, what: str) -> int:
        return self.take(1, what)[0]

    def u16(self, what: str) -> int:
        return int.from_bytes(self.take(2, what), "little")

    def i64(self, what: str) -> int:
        return int.from_bytes(self.take(8, what), "little", signed=True)

    def rest_ok(self) -> bool:
        return self.i == len(self.d)


def decode_module(data: bytes) -> Module:
    """把字节流解码为 Module。任何格式错误都抛 :class:`ToolError`。"""
    if len(data) < 5:
        raise ToolError(PHASE_DECODE, "truncated", "模块长度不足，连头部都不完整")
    r = _Reader(data)
    magic = r.take(4, "magic")
    if magic != MAGIC:
        raise ToolError(
            PHASE_DECODE, "magic",
            f"魔数错误: 期望 {MAGIC!r}，得到 {bytes(magic)!r}",
        )
    count = r.u8("function count")
    if count == 0:
        raise ToolError(PHASE_DECODE, "module.funcs", "模块至少要包含 1 个函数")
    funcs: list[CodeObject] = []
    for _ in range(count):
        funcs.append(_decode_function(r))
    if not r.rest_ok():
        raise ToolError(
            PHASE_DECODE, "trailing.bytes",
            f"模块末尾有 {len(r.d) - r.i} 个多余字节",
        )
    return Module(funcs)


def _decode_function(r: _Reader) -> CodeObject:
    name_len = r.u8("name length")
    if name_len == 0:
        raise ToolError(PHASE_DECODE, "func.name", "函数名长度为 0")
    try:
        name = r.take(name_len, "function name").decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ToolError(
            PHASE_DECODE, "func.name.utf8",
            f"函数名不是合法 UTF-8（{name_len} 字节，offset={r.i - name_len}）",
        )
    ret_tag = r.u8("return tag")
    if ret_tag not in TAG_NAMES:
        raise ToolError(PHASE_DECODE, "type.tag", f"返回类型标签非法: {ret_tag}")
    nparams = r.u8("param count")
    param_tags = []
    for _ in range(nparams):
        t = r.u8("param tag")
        if t not in (TAG_INT, TAG_BOOL):
            raise ToolError(PHASE_DECODE, "type.tag", f"参数类型标签非法: {t}")
        param_tags.append(t)
    nlocals = r.u8("local count")
    local_tags = []
    for _ in range(nlocals):
        t = r.u8("local tag")
        if t not in (TAG_INT, TAG_BOOL):
            raise ToolError(PHASE_DECODE, "type.tag", f"局部变量类型标签非法: {t}")
        local_tags.append(t)
    max_stack = r.u8("max stack")
    code_len = r.u16("code length")
    code = r.take(code_len, "code")
    const_len = r.u16("const count")
    consts: list[tuple[int, int | bool]] = []
    for _ in range(const_len):
        tag = r.u8("const tag")
        if tag == TAG_INT:
            consts.append((TAG_INT, r.i64("int const")))
        elif tag == TAG_BOOL:
            consts.append((TAG_BOOL, r.u8("bool const") != 0))
        else:
            raise ToolError(PHASE_DECODE, "const.tag", f"常量类型标签非法: {tag}")

    instructions = _decode_instructions(code)
    return CodeObject(
        name=name,
        ret_tag=ret_tag,
        param_tags=param_tags,
        local_tags=local_tags,
        instructions=instructions,
        consts=consts,
        max_stack=max_stack,
        declared_code_len=code_len,
    )


def _decode_instructions(code: bytes) -> list[Instruction]:
    ins_list: list[Instruction] = []
    i = 0
    while i < len(code):
        pc = i
        op = code[i]
        if op not in OPERAND_SIZE:
            raise ToolError(
                PHASE_DECODE, "opcode",
                f"非法操作码 0x{op:02x} (offset={pc})", pc=pc,
            )
        width = OPERAND_SIZE[op]
        if i + 1 + width > len(code):
            raise ToolError(
                PHASE_DECODE, "truncated",
                f"offset={pc} 处操作数被截断 (需要 {width} 字节)", pc=pc,
            )
        operand = 0
        if width == 2:
            operand = int.from_bytes(code[i + 1:i + 3], "little")
        ins_list.append(Instruction(opcode=op, operand=operand, pc=pc))
        i += 1 + width
    return ins_list


def disassemble(code: CodeObject) -> str:
    """生成可读反汇编文本。"""
    lines = [
        f"func {code.name} -> {TAG_NAMES.get(code.ret_tag, '?')} "
        f"params={[TAG_NAMES.get(t) for t in code.param_tags]} "
        f"locals={[TAG_NAMES.get(t) for t in code.local_tags]} "
        f"max_stack={code.max_stack}"
    ]
    boundary_pcs = {ins.pc for ins in code.instructions}
    for ins in code.instructions:
        name = OPCODE_NAMES.get(ins.opcode, f"0x{ins.opcode:02x}")
        extra = ""
        if ins.opcode == OP_LOAD_CONST:
            if 0 <= ins.operand < len(code.consts):
                extra = f"  ; {ins.operand} = {code.consts[ins.operand][1]!r}"
        elif ins.opcode in JUMP_OPS:
            mark = " ->" if ins.operand in boundary_pcs else " -> ?(非指令边界)"
            extra = f"{mark} {ins.operand}"
        elif ins.opcode in (OP_LOAD_LOCAL, OP_STORE_LOCAL):
            sname = (
                code.slot_names[ins.operand]
                if ins.operand < len(code.slot_names) else "?"
            )
            extra = f"  ; slot {ins.operand} ({sname})"
        elif ins.opcode == OP_CALL:
            extra = f"  ; func#{ins.operand}"
        lines.append(f"  {ins.pc:4d}: {name:<12s}{ins.operand if OPERAND_SIZE[ins.opcode] else ''}{extra}")
    return "\n".join(lines)
