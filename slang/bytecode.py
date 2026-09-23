"""栈式字节码设计与模块二进制编解码。

指令格式（变长但每条指令长度可确定）
====================================

    操作码 1 字节；操作数随操作码固定，如下表：

    助记符  编码   操作数          栈效果（前 -> 后）          说明
    ------  ----   --------------  --------------------------  --------------------
    PUSH    0x01   imm16(有符号)   -> int
    LOAD    0x02   slot:u8         -> value                    局部量槽
    STORE   0x03   slot:u8         value ->                    局部量槽
    POP     0x04                  a ->
    DUP     0x05                  a -> a a
    ADD     0x06                  int int -> int
    SUB     0x07                  int int -> int
    MUL     0x08                  int int -> int
    DIV     0x09                  int int -> int              除零为运行期错误
    MOD     0x0A                  int int -> int
    EQ      0x0B                  int int -> bool
    NE      0x0C                  int int -> bool
    LT      0x0D                  int int -> bool
    LE      0x0E                  int int -> bool
    GT      0x0F                  int int -> bool
    GE      0x10                  int int -> bool
    AND     0x11                  bool bool -> bool
    OR      0x12                  bool bool -> bool
    NOT     0x13                  bool -> bool
    NEG     0x14                  int -> int
    JUMP    0x15   rel16(有符号)   不变                        pc += rel16（相对操作码）
    JIF     0x16   rel16(有符号)   bool ->                     真则跳，假则顺序
    CALL    0x17   func:u8         args... -> value/空         参数个数由被调函数签名决定
    RET     0x18                  (空栈) ->                    void 返回
    RETV    0x19                  value ->                     带值返回
    PRINT   0x1A                  value ->                     弹栈并打印（int/bool 均可）
    NOP     0x1B                  不变
    TRUE    0x1C                  -> bool(值为真)              布尔常量
    FALSE   0x1D                  -> bool(值为假)              布尔常量

跳转目标合法性（验证器负责，见 verifier.py）：
- 目标必须落在函数代码范围内；
- 目标必须对齐到某条指令的操作码边界；
- 向后跳转（回边）允许，但目标处的栈高度/类型与局部量状态必须能合流。

模块二进制格式（大端序）
========================

    magic   = b"SLANGBC1"               8 字节
    name    : u16 长度 + UTF-8
    source  : u16 长度 + UTF-8          内嵌源码文本（用于报错回显）
    nfuncs  : u16
    func[nfuncs]:
        name       : u8 长度 + ASCII
        nparams    : u8
        ptype[k]   : 每参数 1 字节类型 (1=int,2=bool)
        ret_type   : 1 字节 (0=void,1=int,2=bool)
        nlocals    : u16
        ltype[j]   : 每局部量 1 字节类型（参数后的槽也计入 nlocals）
        codelen    : u32
        code       : codelen 字节原始字节码
        nmap       : u16（调试映射条数）
        map[m]:
            pc     : u32
            start  : u32（源码偏移）
            end    : u32

注意：变异器（mutator.py）只替换 code 字节区域，其余“信封”不变，
从而能覆盖“源码位置仍可回显、但字节码已损坏”的情形。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from .errors import DecodeError

MAGIC = b"SLANGBC1"

# 类型编码
T_VOID = 0
T_INT = 1
T_BOOL = 2

# ---- 操作码 ----
OP_PUSH = 0x01
OP_LOAD = 0x02
OP_STORE = 0x03
OP_POP = 0x04
OP_DUP = 0x05
OP_ADD = 0x06
OP_SUB = 0x07
OP_MUL = 0x08
OP_DIV = 0x09
OP_MOD = 0x0A
OP_EQ = 0x0B
OP_NE = 0x0C
OP_LT = 0x0D
OP_LE = 0x0E
OP_GT = 0x0F
OP_GE = 0x10
OP_AND = 0x11
OP_OR = 0x12
OP_NOT = 0x13
OP_NEG = 0x14
OP_JUMP = 0x15
OP_JIF = 0x16
OP_CALL = 0x17
OP_RET = 0x18
OP_RETV = 0x19
OP_PRINT = 0x1A
OP_NOP = 0x1B
OP_TRUE = 0x1C
OP_FALSE = 0x1D

OPCODE_NAMES: dict[int, str] = {
    OP_PUSH: "PUSH", OP_LOAD: "LOAD", OP_STORE: "STORE", OP_POP: "POP",
    OP_DUP: "DUP", OP_ADD: "ADD", OP_SUB: "SUB", OP_MUL: "MUL",
    OP_DIV: "DIV", OP_MOD: "MOD", OP_EQ: "EQ", OP_NE: "NE",
    OP_LT: "LT", OP_LE: "LE", OP_GT: "GT", OP_GE: "GE",
    OP_AND: "AND", OP_OR: "OR", OP_NOT: "NOT", OP_NEG: "NEG",
    OP_JUMP: "JUMP", OP_JIF: "JIF", OP_CALL: "CALL",
    OP_RET: "RET", OP_RETV: "RETV", OP_PRINT: "PRINT", OP_NOP: "NOP",
    OP_TRUE: "TRUE", OP_FALSE: "FALSE",
}
NAME_TO_OP = {v: k for k, v in OPCODE_NAMES.items()}

# 每个操作码的操作数字节数（不含操作码本身）
OPERAND_WIDTH: dict[int, int] = {
    OP_PUSH: 2, OP_LOAD: 1, OP_STORE: 1, OP_POP: 0, OP_DUP: 0,
    OP_ADD: 0, OP_SUB: 0, OP_MUL: 0, OP_DIV: 0, OP_MOD: 0,
    OP_EQ: 0, OP_NE: 0, OP_LT: 0, OP_LE: 0, OP_GT: 0, OP_GE: 0,
    OP_AND: 0, OP_OR: 0, OP_NOT: 0, OP_NEG: 0,
    OP_JUMP: 2, OP_JIF: 2, OP_CALL: 1,
    OP_RET: 0, OP_RETV: 0, OP_PRINT: 0, OP_NOP: 0,
    OP_TRUE: 0, OP_FALSE: 0,
}

# 比较/算术运算符 -> 操作码（供 compiler 使用）
ARITH_OPS = {
    "+": OP_ADD, "-": OP_SUB, "*": OP_MUL, "/": OP_DIV, "%": OP_MOD,
}
CMP_OPS = {"==": OP_EQ, "!=": OP_NE, "<": OP_LT, "<=": OP_LE, ">": OP_GT, ">=": OP_GE}

MAX_CODE = 0xFFFFFFFF
MAX_SLOT = 255
MAX_FUNCS = 255
MAX_STACK = 256          # 验证期与解释期共同采用的栈高上限
REL_MIN, REL_MAX = -32768, 32767
IMM_MIN, IMM_MAX = -32768, 32767


@dataclass
class Instruction:
    op: int
    pc: int
    operand: int | None = None      # PUSH/JUMP/JIF 的有符号立即数，LOAD/STORE/CALL 的无符号数
    length: int = 1
    src_start: int = -1
    src_end: int = -1

    @property
    def name(self) -> str:
        return OPCODE_NAMES.get(self.op, f"0x{self.op:02x}?")

    def target(self) -> int | None:
        if self.op in (OP_JUMP, OP_JIF):
            return self.pc + (self.operand or 0)
        return None


@dataclass
class DebugEntry:
    pc: int
    src_start: int
    src_end: int


@dataclass
class FuncCode:
    name: str
    param_types: list[int]
    ret_type: int
    local_types: list[int]               # 含参数槽；参数恒为已初始化
    code: bytearray
    debug: list[DebugEntry] = field(default_factory=list)

    # ---- 解码辅助（验证器/解释器共用，解码一次） ----
    def decode(self) -> list[Instruction]:
        """线性解码整条函数。任何损坏都抛 DecodeError。"""
        instrs: list[Instruction] = []
        code = self.code
        n = len(code)
        pc = 0
        dbg = sorted(self.debug, key=lambda d: d.pc)
        di = 0

        def dbg_at(p: int) -> tuple[int, int]:
            nonlocal di
            while di < len(dbg) and dbg[di].pc < p:
                di += 1
            if di < len(dbg) and dbg[di].pc == p:
                return dbg[di].src_start, dbg[di].src_end
            return -1, -1

        while pc < n:
            op = code[pc]
            if op not in OPERAND_WIDTH:
                raise DecodeError(f"函数 {self.name!r}: 非法操作码 0x{op:02x} @pc={pc}")
            width = OPERAND_WIDTH[op]
            if pc + 1 + width > n:
                raise DecodeError(
                    f"函数 {self.name!r}: @pc={pc} 的 {OPCODE_NAMES[op]} 操作数被截断"
                    f"（需要 {width} 字节）")
            operand: int | None = None
            length = 1 + width
            if op == OP_PUSH:
                operand = struct.unpack_from(">h", code, pc + 1)[0]
            elif op in (OP_JUMP, OP_JIF):
                operand = struct.unpack_from(">h", code, pc + 1)[0]
            elif op in (OP_LOAD, OP_STORE, OP_CALL):
                operand = code[pc + 1]
            ss, se = dbg_at(pc)
            instrs.append(Instruction(op, pc, operand, length, ss, se))
            pc += length
        return instrs

    def debug_at(self, pc: int) -> DebugEntry | None:
        """最近的、不晚于 pc 的调试项（用于把路径节点映射回源码）。"""
        best: DebugEntry | None = None
        for d in self.debug:
            if d.pc <= pc:
                best = d
            else:
                break
        return best


@dataclass
class Module:
    name: str
    source: str
    funcs: list[FuncCode]

    # ---- 便捷查找 ----
    def func_index(self, name: str) -> int:
        for i, f in enumerate(self.funcs):
            if f.name == name:
                return i
        return -1

    # ---- 序列化 ----
    def encode(self) -> bytes:
        if len(self.funcs) > MAX_FUNCS:
            raise ValueError("函数数量超过 255")
        out = bytearray(MAGIC)
        _put_str16(out, self.name)
        _put_str16(out, self.source)
        out += struct.pack(">H", len(self.funcs))
        for f in self.funcs:
            if len(f.name) > 255:
                raise ValueError("函数名过长")
            out.append(len(f.name))
            out += f.name.encode("ascii")
            if len(f.param_types) > 255:
                raise ValueError(f"{f.name}: 参数过多")
            out.append(len(f.param_types))
            out += bytes(f.param_types)
            out.append(f.ret_type)
            if len(f.local_types) > 0xFFFF:
                raise ValueError(f"{f.name}: 局部量过多")
            out += struct.pack(">H", len(f.local_types))
            out += bytes(f.local_types)
            if len(f.code) > MAX_CODE:
                raise ValueError(f"{f.name}: 代码过长")
            out += struct.pack(">I", len(f.code))
            out += f.code
            out += struct.pack(">H", len(f.debug))
            for d in f.debug:
                out += struct.pack(">III", d.pc, d.src_start, d.src_end)
        return bytes(out)

    def replace_code(self, func_index: int, code: bytes | bytearray) -> bytes:
        """变异用：只替换某函数的 code 区域，重新序列化整个信封。"""
        old = self.funcs[func_index].code
        self.funcs[func_index].code = bytearray(code)
        try:
            return self.encode()
        finally:
            self.funcs[func_index].code = old


# =====================================================================
# 解码
# =====================================================================
class _Reader:
    def __init__(self, data: bytes) -> None:
        self.d = data
        self.p = 0

    def take(self, n: int, what: str) -> bytes:
        if self.p + n > len(self.d):
            raise DecodeError(f"模块截断：读取 {what} 需要 {n} 字节 @offset={self.p}")
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def u8(self, what: str) -> int:
        return self.take(1, what)[0]

    def u16(self, what: str) -> int:
        return struct.unpack(">H", self.take(2, what))[0]

    def u32(self, what: str) -> int:
        return struct.unpack(">I", self.take(4, what))[0]

    def s16(self, what: str) -> int:
        return struct.unpack(">h", self.take(2, what))[0]


def _put_str16(out: bytearray, s: str) -> None:
    b = s.encode("utf-8")
    if len(b) > 0xFFFF:
        raise ValueError("字符串过长(>65535 字节)")
    out += struct.pack(">H", len(b))
    out += b


def _get_str16(r: _Reader, what: str) -> str:
    n = r.u16(what + " 长度")
    return r.take(n, what).decode("utf-8")


def decode_module(data: bytes) -> Module:
    if len(data) < 8:
        raise DecodeError("文件过小，缺少魔数")
    if data[:8] != MAGIC:
        raise DecodeError(f"魔数错误：期望 {MAGIC!r}，实际 {data[:8]!r}")
    r = _Reader(data)
    r.p = 8
    name = _get_str16(r, "模块名")
    source = _get_str16(r, "源码文本")
    nfuncs = r.u16("函数数量")
    funcs: list[FuncCode] = []
    for fi in range(nfuncs):
        nl = r.u8("函数名长度")
        fname = r.take(nl, "函数名").decode("ascii")
        nparams = r.u8("参数数量")
        ptypes = list(r.take(nparams, "参数类型"))
        for t in ptypes:
            if t not in (T_INT, T_BOOL):
                raise DecodeError(f"函数 {fname}: 非法参数类型 {t}")
        ret = r.u8("返回类型")
        if ret not in (T_VOID, T_INT, T_BOOL):
            raise DecodeError(f"函数 {fname}: 非法返回类型 {ret}")
        nlocals = r.u16("局部量数量")
        ltypes = list(r.take(nlocals, "局部量类型"))
        for t in ltypes:
            if t not in (T_INT, T_BOOL):
                raise DecodeError(f"函数 {fname}: 非法局部量类型 {t}")
        if nparams > nlocals:
            raise DecodeError(f"函数 {fname}: 参数数 {nparams} 多于局部槽 {nlocals}")
        codelen = r.u32("代码长度")
        code = bytearray(r.take(codelen, "函数字节码"))
        nmap = r.u16("调试映射数量")
        dbg: list[DebugEntry] = []
        for _ in range(nmap):
            pc, ss, se = struct.unpack(">III", r.take(12, "调试映射项"))
            dbg.append(DebugEntry(pc, ss, se))
        fc = FuncCode(fname, ptypes, ret, ltypes, code, dbg)
        funcs.append(fc)
    if r.p != len(data):
        raise DecodeError(f"模块尾部有 {len(data) - r.p} 个多余字节")
    return Module(name, source, funcs)
