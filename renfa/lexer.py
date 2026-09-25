"""词法分析器（手写，不使用任何现成正则库）。

支持的 token：
  ( ) | * + ? . ^ $
  普通码点字面量（含转义产生的码点）
  {n} {n,} {n,m}   —— 只能作为有限重复量词出现；不合法的花括号一律报错，
                      要匹配字面 ``{``/``}`` 请写 ``\\{`` / ``\\}``。

转义：
  \\n \\t \\r \\f \\v   常见控制字符
  \\uXXXX  与  \\u{XXXX} Unicode 码点转义（拒绝代理码点 D800–DFFF）
  对任意标点字符转义即表示该字面量（如 \\. \\( \\\\）

明确不支持（词法期即报错并定位）：
  \\d \\w \\s \\b 等字符类/边界简写；[..] 字符类。
"""

from dataclasses import dataclass

from .errors import RegexSyntaxError
from .source import Source, Span

# token 类型常量
T_LPAREN = "LPAREN"
T_RPAREN = "RPAREN"
T_PIPE = "PIPE"
T_STAR = "STAR"
T_PLUS = "PLUS"
T_QUESTION = "QUESTION"
T_DOT = "DOT"
T_ANCHOR_START = "ANCHOR_START"
T_ANCHOR_END = "ANCHOR_END"
T_LITERAL = "LITERAL"
T_REPEAT = "REPEAT"
T_EOF = "EOF"

_SIMPLE_ESCAPES = {
    "n": 0x0A,
    "t": 0x09,
    "r": 0x0D,
    "f": 0x0C,
    "v": 0x0B,
    "0": 0x00,
}

_UNSUPPORTED_ESCAPES = {
    "d": r"\d 字符类简写不属于本引擎支持的正则子集",
    "D": r"\D 字符类简写不属于本引擎支持的正则子集",
    "w": r"\w 字符类简写不属于本引擎支持的正则子集",
    "W": r"\W 字符类简写不属于本引擎支持的正则子集",
    "s": r"\s 字符类简写不属于本引擎支持的正则子集",
    "S": r"\S 字符类简写不属于本引擎支持的正则子集",
    "b": r"\b 词边界不属于本引擎支持的正则子集（仅支持 ^ 与 $ 锚点）",
    "B": r"\B 词边界不属于本引擎支持的正则子集（仅支持 ^ 与 $ 锚点）",
}

_HEX = set("0123456789abcdefABCDEF")


@dataclass
class Token:
    kind: str
    span: Span
    # LITERAL: 码点（int）；REPEAT: (min, max) 元组，None 表示无限上界
    value: object = None
    raw: str = ""  # REPEAT 的原文，如 "{2,3}"

    @property
    def codepoint(self) -> int:
        return self.value  # type: ignore[return-value]


class Lexer:
    def __init__(self, source: Source):
        self.src = source
        self.cs = source.codepoints
        self.pos = 0

    # ---- 基础工具 ----

    def _peek(self, ahead: int = 0) -> str:
        j = self.pos + ahead
        if j < len(self.cs):
            return self.cs[j]
        return ""

    def _error(self, start: int, end: int, msg: str) -> RegexSyntaxError:
        return RegexSyntaxError(msg, self.src.span(start, end), self.src)

    def _read_unicode_escape(self, start: int) -> tuple[int, int]:
        """读 \\uXXXX 或 \\u{XXXX}，返回 (码点, 下一位置)。调用时 self.pos 指向 'u'。"""
        p = self.pos + 1  # 跳过 u
        if p < len(self.cs) and self.cs[p] == "{":
            # \u{HHHH}
            q = p + 1
            digits = []
            while q < len(self.cs) and self.cs[q] in _HEX:
                digits.append(self.cs[q])
                q += 1
            if not digits or q >= len(self.cs) or self.cs[q] != "}":
                raise self._error(start, min(q + 1, len(self.cs)),
                                  r"无效的 \u{...} Unicode 转义：需要 1–6 个十六进制数字并以 } 结束")
            cp = int("".join(digits), 16)
            return cp, q + 1
        digits = self.cs[p:p + 4]
        if len(digits) < 4 or any(c not in _HEX for c in digits):
            raise self._error(start, min(p + 4, len(self.cs)),
                              r"无效的 \uXXXX Unicode 转义：需要恰好 4 个十六进制数字")
        return int("".join(digits), 16), p + 4

    def _read_escape(self) -> Token:
        """读以 \\ 起始的转义，self.pos 指向反斜杠。"""
        start = self.pos
        self.pos += 1
        if self.pos >= len(self.cs):
            raise self._error(start, len(self.cs), "反斜杠 \\ 后面缺少被转义字符")
        ch = self.cs[self.pos]
        if ch == "u":
            cp, nxt = self._read_unicode_escape(start)
            self._check_cp(cp, start, nxt)
            tok = Token(T_LITERAL, self.src.span(start, nxt), cp)
            self.pos = nxt
            return tok
        if ch in _UNSUPPORTED_ESCAPES:
            end = self.pos + 1
            raise self._error(start, end, _UNSUPPORTED_ESCAPES[ch])
        if ch in "123456789":
            end = self.pos + 1
            raise self._error(
                start, end,
                f"\\{ch} 看起来是反向引用/数字转义；本引擎不支持反向引用"
                "（捕获组不属于该正则子集）")
        if ch in _SIMPLE_ESCAPES:
            cp = _SIMPLE_ESCAPES[ch]
        else:
            # 其它任何字符（含标点与字母）转义后按其字面码点处理。
            cp = ord(ch)
        self.pos += 1
        return Token(T_LITERAL, self.src.span(start, self.pos), cp)

    def _check_cp(self, cp: int, start: int, end: int) -> None:
        if cp > 0x10FFFF:
            raise self._error(start, end, f"Unicode 码点 U+{cp:X} 超出最大值 U+10FFFF")
        if 0xD800 <= cp <= 0xDFFF:
            raise self._error(start, end,
                              f"U+{cp:04X} 是 UTF-16 代理码点，不是合法的 Unicode 标量值")

    def _read_repeat_quantifier(self) -> Token:
        """尝试把 {..} 读成重复量词；失败则报错（字面花括号必须转义）。"""
        start = self.pos
        n = len(self.cs)
        p = start + 1  # 跳过 {
        if p >= n or self.cs[p] not in "0123456789":
            raise self._error(start, min(start + 1, n),
                              "无效的花括号：量词必须形如 {n}、{n,} 或 {n,m}；"
                              "要匹配字面 '{' 请写 \\{")
        digits = []
        while p < n and self.cs[p] in "0123456789":
            digits.append(self.cs[p])
            p += 1
        lo = int("".join(digits)) if digits else None
        hi = lo
        if p < n and self.cs[p] == "}":
            # {n}
            p += 1
        elif p < n and self.cs[p] == ",":
            p += 1
            d2 = []
            while p < n and self.cs[p] in "0123456789":
                d2.append(self.cs[p])
                p += 1
            if p >= n or self.cs[p] != "}":
                raise self._error(start, min(p + 1, n),
                                  "重复量词缺少右花括号 '}'")
            if d2:
                hi = int("".join(d2))
            else:
                hi = None  # {n,} 上界无限
            p += 1
        else:
            raise self._error(start, min(p + 1, n),
                              "无效的重复量词：需要 } 或 ,（形如 {n}、{n,}、{n,m}）")
        end = p
        if hi is not None and lo > hi:
            raise self._error(start, end, f"重复量词下界 {lo} 大于上界 {hi}")
        if lo > 1_000_000 or (hi is not None and hi > 1_000_000):
            raise self._error(start, end, "重复量词数值超过 1,000,000 的上限")
        raw = "".join(self.cs[start:end])
        self.pos = end
        return Token(T_REPEAT, self.src.span(start, end), (lo, hi), raw)

    # ---- 主循环 ----

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        punctuation = {
            "(": T_LPAREN,
            ")": T_RPAREN,
            "|": T_PIPE,
            "*": T_STAR,
            "+": T_PLUS,
            "?": T_QUESTION,
            ".": T_DOT,
            "^": T_ANCHOR_START,
            "$": T_ANCHOR_END,
        }
        while self.pos < len(self.cs):
            ch = self.cs[self.pos]
            start = self.pos
            if ch == "\\":
                tokens.append(self._read_escape())
                continue
            if ch == "{":
                tokens.append(self._read_repeat_quantifier())
                continue
            if ch == "}":
                raise self._error(start, start + 1,
                                  "多余的右花括号 '}'；要匹配它请写 \\}")
            if ch == "[":
                raise self._error(
                    start, start + 1,
                    "字符类 [..] 不属于本引擎支持的正则子集；"
                    "要匹配字面 '[' 请写 \\[，需要候选集请用选择 (a|b|c)")
            if ch == "]":
                raise self._error(start, start + 1,
                                  "多余的 ']'；要匹配它请写 \\]")
            if ch in punctuation:
                kind = punctuation[ch]
                self.pos += 1
                tokens.append(Token(kind, self.src.span(start, self.pos)))
                continue
            # 普通字面码点
            self.pos += 1
            tokens.append(Token(T_LITERAL, self.src.span(start, self.pos), ord(ch)))
        tokens.append(Token(T_EOF, self.src.span(len(self.cs), len(self.cs))))
        return tokens


def tokenize(pattern: str) -> tuple[Source, list[Token]]:
    src = Source(pattern)
    return src, Lexer(src).tokenize()
