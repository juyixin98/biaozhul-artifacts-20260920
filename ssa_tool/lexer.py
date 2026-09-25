"""ToyLang 词法分析器。

扫描字符流，输出带源码位置（文件名/行/列/偏移）的 Token。
位置信息会跟随 AST 一直保留到 IR 指令上，解释执行期报错可回溯到源码。
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import LexError

# 关键字表
KEYWORDS = {
    "func",
    "var",
    "if",
    "else",
    "while",
    "return",
    "true",
    "false",
}

# 双字符运算符优先于单字符
TWO_CHAR = {"==", "!=", "<=", ">=", "&&", "||"}
ONE_CHAR = set("+-*/%(){};,<>!=?:")


@dataclass(frozen=True)
class Token:
    kind: str          # INT / IDENT / KEYWORD / OP / EOF
    value: str
    line: int
    col: int
    offset: int
    file: str = "<src>"

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"Token({self.kind}, {self.value!r}, {self.line}:{self.col})"


class Lexer:
    def __init__(self, source: str, file: str = "<src>"):
        self.src = source
        self.file = file
        self.n = len(source)
        self.i = 0
        self.line = 1
        self.col = 1

    # ---- 位置辅助 ----
    def _peek(self, off: int = 0) -> str:
        j = self.i + off
        return self.src[j] if j < self.n else ""

    def _advance(self) -> str:
        ch = self.src[self.i]
        self.i += 1
        if ch == "\n":
            self.line += 1
            self.col = 1
        else:
            self.col += 1
        return ch

    def _skip_trivia(self) -> None:
        while self.i < self.n:
            ch = self._peek()
            if ch in " \t\r\n":
                self._advance()
            elif ch == "/" and self._peek(1) == "/":
                while self.i < self.n and self._peek() != "\n":
                    self._advance()
            elif ch == "/" and self._peek(1) == "*":
                start_line, start_col = self.line, self.col
                self._advance(); self._advance()
                closed = False
                while self.i < self.n:
                    if self._peek() == "*" and self._peek(1) == "/":
                        self._advance(); self._advance()
                        closed = True
                        break
                    self._advance()
                if not closed:
                    raise LexError("块注释未闭合", start_line, start_col)
            else:
                return

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        while True:
            self._skip_trivia()
            if self.i >= self.n:
                tokens.append(Token("EOF", "", self.line, self.col, self.i, self.file))
                return tokens

            line, col, off = self.line, self.col, self.i
            ch = self._peek()

            if ch.isdigit():
                text = self._read_number()
                tokens.append(Token("INT", text, line, col, off, self.file))
            elif ch.isalpha() or ch == "_":
                text = self._read_ident()
                kind = "KEYWORD" if text in KEYWORDS else "IDENT"
                tokens.append(Token(kind, text, line, col, off, self.file))
            else:
                two = self._peek() + self._peek(1)
                if two in TWO_CHAR:
                    self._advance(); self._advance()
                    tokens.append(Token("OP", two, line, col, off, self.file))
                elif ch in ONE_CHAR:
                    self._advance()
                    tokens.append(Token("OP", ch, line, col, off, self.file))
                else:
                    raise LexError(f"无法识别的字符 {ch!r}", line, col)

    def _read_number(self) -> str:
        start = self.i
        while self._peek().isdigit():
            self._advance()
        return self.src[start:self.i]

    def _read_ident(self) -> str:
        start = self.i
        while self._peek().isalnum() or self._peek() == "_":
            self._advance()
        return self.src[start:self.i]


def tokenize(source: str, file: str = "<src>") -> list[Token]:
    return Lexer(source, file).tokenize()
