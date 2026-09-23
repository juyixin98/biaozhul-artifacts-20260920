"""手写词法分析器（不使用任何现成编译器/解析框架）。"""

from __future__ import annotations

from dataclasses import dataclass

from .source import LexError, SourceText

# 关键字
KEYWORDS = {"if", "else", "while", "print", "true", "false"}

# 多字符运算符（长的在前，保证最大匹配）
MULTI_OPS = ("==", "!=", "<=", ">=", "&&", "||")
SINGLE_OPS = set("+-*/%<>!=(){};")


@dataclass(frozen=True)
class Token:
    kind: str          # INT / IDENT / KEYWORD / PUNCT / EOF
    text: str
    start: int
    end: int


class Lexer:
    def __init__(self, source: SourceText):
        self.src = source
        self.text = source.text

    def tokenize(self) -> list[Token]:
        tokens: list[Token] = []
        i, n = 0, len(self.text)
        while i < n:
            ch = self.text[i]
            # 空白
            if ch in " \t\r\n":
                i += 1
                continue
            # 行注释 //...
            if ch == "/" and i + 1 < n and self.text[i + 1] == "/":
                i += 2
                while i < n and self.text[i] != "\n":
                    i += 1
                continue
            # 整数
            if ch.isdigit():
                start = i
                while i < n and self.text[i].isdigit():
                    i += 1
                tokens.append(Token("INT", self.text[start:i], start, i))
                continue
            # 标识符 / 关键字
            if ch.isalpha() or ch == "_":
                start = i
                while i < n and (self.text[i].isalnum() or self.text[i] == "_"):
                    i += 1
                word = self.text[start:i]
                kind = "KEYWORD" if word in KEYWORDS else "IDENT"
                tokens.append(Token(kind, word, start, i))
                continue
            # 多字符运算符
            two = self.text[i:i + 2]
            if two in MULTI_OPS:
                tokens.append(Token("PUNCT", two, i, i + 2))
                i += 2
                continue
            # 单字符
            if ch in SINGLE_OPS:
                tokens.append(Token("PUNCT", ch, i, i + 1))
                i += 1
                continue
            sp = self.src.span(i, i + 1)
            raise LexError(f"unexpected character {ch!r}", sp, self.src)
        tokens.append(Token("EOF", "", n, n))
        return tokens
