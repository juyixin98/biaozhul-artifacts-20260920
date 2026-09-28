"""正则语法解析：模式串 -> AST。

支持的子集（文档化契约）：
- 字面字符与转义：\\n \\t \\r \\f \\v \\\\ 及转义元字符、\\xHH、\\uHHHH
- 字符类：[abc]、[a-z]、[^...]（对固定字母表取补）；类内 \\n 等转义可用
- 连接、| 选择、( ) 分组
- 后缀：* + ? {m} {m,} {m,n}（上界受 Limits.max_repeat 约束）
- `.`：除换行（\\n）外任意字符（换行模式固定 LF）

语法错误抛 InputError（category=input_error），带位置信息。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import NEWLINE_CP, Limits
from app.errors import InputError, ResourceExhausted
from app.kernel.charset import CharSet

# ---- AST ----


@dataclass(frozen=True)
class Char:
    cs: CharSet


@dataclass(frozen=True)
class Concat:
    parts: tuple["Node", ...]


@dataclass(frozen=True)
class Alt:
    options: tuple["Node", ...]


@dataclass(frozen=True)
class Repeat:
    node: "Node"
    min: int
    max: int | None  # None 表示无界


Node = Char | Concat | Alt | Repeat

_META = set("()[]{}|*+?.\\")
_ESCAPES = {
    "n": 0x0A,
    "t": 0x09,
    "r": 0x0D,
    "f": 0x0C,
    "v": 0x0B,
    "0": 0x00,
}


def _err(pattern: str, pos: int, msg: str) -> InputError:
    return InputError(
        code="REGEX_SYNTAX",
        message=f"正则语法错误（位置 {pos}）: {msg}",
        details={"pattern": pattern, "position": pos, "reason": msg},
    )


class _Parser:
    def __init__(self, pattern: str, limits: Limits):
        self.s = pattern
        self.i = 0
        self.limits = limits

    # -- 基础 --
    def peek(self) -> str | None:
        return self.s[self.i] if self.i < len(self.s) else None

    def next(self) -> str:
        ch = self.peek()
        if ch is None:
            raise _err(self.s, self.i, "意外结束")
        self.i += 1
        return ch

    def expect(self, ch: str) -> None:
        got = self.next()
        if got != ch:
            raise _err(self.s, self.i - 1, f"期望 {ch!r}，得到 {got!r}")

    # -- 文法 --
    def parse_alt(self) -> Node:
        options = [self.parse_concat()]
        while self.peek() == "|":
            self.next()
            options.append(self.parse_concat())
        if len(options) == 1:
            return options[0]
        return Alt(tuple(options))

    def parse_concat(self) -> Node:
        parts: list[Node] = []
        while True:
            ch = self.peek()
            if ch is None or ch in "|)":
                break
            parts.append(self.parse_repeat())
        if not parts:
            return Concat(())  # 空分支（可匹配空串，由上层规则校验拦截）
        if len(parts) == 1:
            return parts[0]
        return Concat(tuple(parts))

    def parse_repeat(self) -> Node:
        atom = self.parse_atom()
        while True:
            ch = self.peek()
            if ch == "*":
                self.next()
                atom = Repeat(atom, 0, None)
            elif ch == "+":
                self.next()
                atom = Repeat(atom, 1, None)
            elif ch == "?":
                self.next()
                atom = Repeat(atom, 0, 1)
            elif ch == "{":
                atom = self.parse_brace_repeat(atom)
            else:
                break
        return atom

    def parse_brace_repeat(self, atom: Node) -> Node:
        start = self.i
        self.expect("{")
        lo = self.parse_number()
        if self.peek() == "}":
            self.next()
            hi: int | None = lo
        else:
            self.expect(",")
            if self.peek() == "}":
                hi = None
            else:
                hi = self.parse_number()
            self.expect("}")
        if hi is not None and hi < lo:
            raise _err(self.s, start, f"重复上界 {hi} 小于下界 {lo}")
        cap = self.limits.max_repeat
        if lo > cap or (hi is not None and hi > cap):
            raise ResourceExhausted(
                code="REPEAT_TOO_LARGE",
                message=f"重复次数超过上限 {cap}",
                details={"pattern": self.s, "position": start, "min": lo, "max": hi, "limit": cap},
            )
        return Repeat(atom, lo, hi)

    def parse_number(self) -> int:
        start = self.i
        while self.peek() is not None and self.peek().isdigit():
            self.next()
        if self.i == start:
            raise _err(self.s, start, "期望数字")
        return int(self.s[start : self.i])

    def parse_atom(self) -> Node:
        ch = self.peek()
        if ch is None:
            raise _err(self.s, self.i, "期望原子表达式，已结束")
        if ch == "(":
            self.next()
            node = self.parse_alt()
            if self.peek() != ")":
                raise _err(self.s, self.i, "缺少右括号 )")
            self.next()
            return node
        if ch == "[":
            return Char(self.parse_class())
        if ch == ".":
            self.next()
            # 换行模式固定：`.` 不匹配 \n
            return Char(CharSet.of_char(NEWLINE_CP).complement())
        if ch == "\\":
            return Char(CharSet.of_char(self.parse_escape()))
        if ch in "*+?{}":
            raise _err(self.s, self.i, f"量词 {ch!r} 缺少前置原子")
        if ch in ")|":
            raise _err(self.s, self.i, f"意外的 {ch!r}")
        self.next()
        return Char(CharSet.of_char(ord(ch)))

    def parse_escape(self) -> int:
        """消费 `\\x` 形式的转义，返回码点。调用时游标位于反斜杠。"""
        pos = self.i
        self.expect("\\")
        ch = self.next()
        if ch in _ESCAPES:
            return _ESCAPES[ch]
        if ch == "x":
            return self.parse_hex(pos, 2)
        if ch == "u":
            return self.parse_hex(pos, 4)
        if ch in _META or ch in "-^]/":
            return ord(ch)
        raise _err(self.s, pos, f"未知转义 \\{ch}")

    def parse_hex(self, esc_pos: int, digits: int) -> int:
        if self.i + digits > len(self.s):
            raise _err(self.s, esc_pos, "十六进制转义位数不足")
        raw = self.s[self.i : self.i + digits]
        if any(c not in "0123456789abcdefABCDEF" for c in raw):
            raise _err(self.s, esc_pos, f"非法十六进制转义: {raw!r}")
        self.i += digits
        cp = int(raw, 16)
        if cp > 0x10FFFF:
            raise _err(self.s, esc_pos, f"码点超出固定字母表: U+{cp:X}")
        return cp

    def parse_class(self) -> CharSet:
        self.expect("[")
        negate = False
        if self.peek() == "^":
            self.next()
            negate = True
        intervals: list[tuple[int, int]] = []
        first = True
        while True:
            ch = self.peek()
            if ch is None:
                raise _err(self.s, self.i, "字符类未闭合，缺少 ]")
            if ch == "]" and not first:
                self.next()
                break
            first = False
            lo = self.parse_class_char()
            if self.peek() == "-" and self.i + 1 < len(self.s) and self.s[self.i + 1] != "]":
                self.next()  # '-'
                hi = self.parse_class_char()
                if hi < lo:
                    raise _err(self.s, self.i, f"字符类区间倒置: {chr(lo)!r}-{chr(hi)!r}")
                intervals.append((lo, hi))
            else:
                intervals.append((lo, lo))
        cs = CharSet.normalize(intervals)
        return cs.complement() if negate else cs

    def parse_class_char(self) -> int:
        if self.peek() == "\\":
            return self.parse_escape()
        ch = self.next()
        if ch == "[":
            raise _err(self.s, self.i - 1, "字符类内不允许嵌套 [")
        return ord(ch)


def parse_pattern(pattern: str, limits: Limits) -> Node:
    if not pattern:
        raise InputError(
            code="EMPTY_PATTERN",
            message="模式串为空",
            details={"pattern": pattern},
        )
    if len(pattern) > limits.max_pattern_len:
        raise ResourceExhausted(
            code="PATTERN_TOO_LONG",
            message=f"模式长度 {len(pattern)} 超过上限 {limits.max_pattern_len}",
            details={"length": len(pattern), "limit": limits.max_pattern_len},
        )
    parser = _Parser(pattern, limits)
    node = parser.parse_alt()
    if parser.peek() is not None:
        raise _err(pattern, parser.i, f"存在多余字符 {parser.peek()!r}")
    return node
