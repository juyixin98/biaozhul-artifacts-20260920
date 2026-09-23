"""词法分析器。

职责
====
1. 把源码切成令牌流，保留每个令牌的源码位置（半开区间 ``[start, end)``）；
2. 识别字符串字面量（双引号、单引号）并在字符串内部**不**产生任何
   运算符/括号令牌——这是“字符串内符号”用例的关键；
3. 词法错误（未闭合字符串、非法字符）不抛异常，作为 ``LexError``
   附着在令牌上；解析器照常继续，最终报告的错误位置与全量解析一致。

设计要点：令牌的 ``is_lex_error`` 为真时，增量解析中任何覆盖该令牌的
旧节点都不允许复用（字符串可能被后续编辑“补全”，语义会变）。
"""

from __future__ import annotations

from dataclasses import dataclass

KEYWORDS = {
    "var", "fn", "return", "if", "else", "while", "break", "continue",
    "true", "false", "null",
}

# 多字符运算符按长度优先匹配
MULTI_OPS = ("==", "!=", "<=", ">=", "&&", "||")
SINGLE_OPS = set("+-*/%<>=!(){},;[]")


@dataclass(slots=True)
class Token:
    kind: str          # "keyword" | "ident" | "number" | "string" | "op" | "eof"
    text: str          # 原文（字符串令牌包含外层引号）
    start: int
    end: int
    is_lex_error: bool = False

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "text": self.text,
            "start": self.start,
            "end": self.end,
            "lex_error": self.is_lex_error,
        }


@dataclass(slots=True)
class LexError:
    message: str
    start: int
    end: int

    def to_dict(self) -> dict:
        return {"message": self.message, "start": self.start, "end": self.end}


def _is_ident_start(c: str) -> bool:
    return c.isalpha() or c == "_"


def _is_ident_part(c: str) -> bool:
    return c.isalnum() or c == "_"


def tokenize(source: str) -> tuple[list[Token], list[LexError]]:
    """把源码切分为令牌列表（末尾追加一个 EOF 哨兵）与词法错误列表。"""
    tokens: list[Token] = []
    errors: list[LexError] = []
    n = len(source)
    i = 0

    while i < n:
        c = source[i]

        # 空白
        if c in " \t\r\n":
            i += 1
            continue

        # 行注释 //
        if c == "/" and i + 1 < n and source[i + 1] == "/":
            i += 2
            while i < n and source[i] != "\n":
                i += 1
            continue

        # 标识符 / 关键字
        if _is_ident_start(c):
            start = i
            i += 1
            while i < n and _is_ident_part(source[i]):
                i += 1
            word = source[start:i]
            kind = "keyword" if word in KEYWORDS else "ident"
            tokens.append(Token(kind, word, start, i))
            continue

        # 数字（仅整数，不支持小数点）
        if c.isdigit():
            start = i
            i += 1
            while i < n and source[i].isdigit():
                i += 1
            tokens.append(Token("number", source[start:i], start, i))
            continue

        # 字符串：内部的括号/运算符等一律不切词
        if c == '"' or c == "'":
            start = i
            quote = c
            i += 1
            value_chars: list[str] = []
            closed = False
            while i < n:
                ch = source[i]
                if ch == "\\" and i + 1 < n:
                    # 转义：\" \' \\ \n \t
                    value_chars.append(source[i:i + 2])
                    i += 2
                    continue
                if ch == quote:
                    closed = True
                    i += 1  # 吃掉闭合引号
                    break
                if ch == "\n":
                    break  # 不允许跨行，交由未闭合逻辑处理
                value_chars.append(ch)
                i += 1
            if not closed:
                # 未闭合字符串：令牌从开头引号一直延伸到源码尾/行尾
                end = i
                text = source[start:end]
                errors.append(LexError("未闭合的字符串字面量", start, end))
                tokens.append(Token("string", text, start, end, is_lex_error=True))
            else:
                text = source[start:i]
                tokens.append(Token("string", text, start, i))
            continue

        # 多字符 / 单字符运算符与括号
        two = source[i:i + 2]
        if two in MULTI_OPS:
            tokens.append(Token("op", two, i, i + 2))
            i += 2
            continue
        if c in SINGLE_OPS:
            tokens.append(Token("op", c, i, i + 1))
            i += 1
            continue

        # 非法字符：产生一个错误令牌，保证它在源码中占住位置
        errors.append(LexError(f"非法字符 {c!r}", i, i + 1))
        tokens.append(Token("op", c, i, i + 1, is_lex_error=True))
        i += 1

    tokens.append(Token("eof", "", n, n))
    return tokens, errors
