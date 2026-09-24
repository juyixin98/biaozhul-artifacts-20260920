package com.tvl.sql;

import java.util.ArrayList;
import java.util.List;

/**
 * 极简词法分析器。Token 类型：
 *  关键字/标识符（IDENT）、数字（NUMBER）、单引号字符串（STRING）、
 *  参数 ?（PARAM）、比较/括号/逗号/星号等符号。
 * 关键字与标识符的区分在解析器里按字面做（大小写不敏感）。
 */
final class Lexer {

    enum Kind {
        IDENT, NUMBER, STRING, PARAM,
        LPAREN, RPAREN, COMMA, STAR,
        OP, // = <> != < <= > >=
        EOF
    }

    static final class Token {
        final Kind kind;
        final String text;
        final int pos;

        Token(Kind kind, String text, int pos) {
            this.kind = kind;
            this.text = text;
            this.pos = pos;
        }

        @Override
        public String toString() {
            return kind + "(" + text + ")@" + pos;
        }
    }

    private final String src;
    private int pos;

    private Lexer(String src) {
        this.src = src;
    }

    static List<Token> tokenize(String sql) {
        return new Lexer(sql).run();
    }

    private List<Token> run() {
        List<Token> tokens = new ArrayList<>();
        while (true) {
            skipWhitespaceAndComments();
            if (pos >= src.length()) {
                tokens.add(new Token(Kind.EOF, "", pos));
                return tokens;
            }
            char c = src.charAt(pos);
            int start = pos;
            if (c == '?') {
                pos++;
                tokens.add(new Token(Kind.PARAM, "?", start));
            } else if (c == '(') {
                pos++;
                tokens.add(new Token(Kind.LPAREN, "(", start));
            } else if (c == ')') {
                pos++;
                tokens.add(new Token(Kind.RPAREN, ")", start));
            } else if (c == ',') {
                pos++;
                tokens.add(new Token(Kind.COMMA, ",", start));
            } else if (c == '*') {
                pos++;
                tokens.add(new Token(Kind.STAR, "*", start));
            } else if (c == '\'') {
                tokens.add(readString());
            } else if (Character.isDigit(c) || (c == '.' && pos + 1 < src.length()
                    && Character.isDigit(src.charAt(pos + 1)))) {
                tokens.add(readNumber());
            } else if (isIdentStart(c)) {
                tokens.add(readIdent());
            } else if (c == '=' || c == '<' || c == '>' || c == '!') {
                tokens.add(readOperator());
            } else {
                throw new SqlParseException("无法识别的字符 '" + c + "'（位置 " + pos + "）");
            }
        }
    }

    private void skipWhitespaceAndComments() {
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (Character.isWhitespace(c)) {
                pos++;
            } else if (c == '-' && pos + 1 < src.length() && src.charAt(pos + 1) == '-') {
                pos += 2;
                while (pos < src.length() && src.charAt(pos) != '\n') {
                    pos++;
                }
            } else if (c == '/' && pos + 1 < src.length() && src.charAt(pos + 1) == '*') {
                pos += 2;
                while (pos + 1 < src.length()
                        && !(src.charAt(pos) == '*' && src.charAt(pos + 1) == '/')) {
                    pos++;
                }
                if (pos + 1 >= src.length()) {
                    throw new SqlParseException("块注释没有闭合");
                }
                pos += 2;
            } else {
                return;
            }
        }
    }

    private Token readString() {
        int start = pos;
        pos++; // 开引号
        StringBuilder sb = new StringBuilder();
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c == '\'') {
                // SQL 转义：'' -> '
                if (pos + 1 < src.length() && src.charAt(pos + 1) == '\'') {
                    sb.append('\'');
                    pos += 2;
                } else {
                    pos++;
                    return new Token(Kind.STRING, sb.toString(), start);
                }
            } else {
                sb.append(c);
                pos++;
            }
        }
        throw new SqlParseException("字符串缺少闭合单引号（位置 " + start + "）");
    }

    private Token readNumber() {
        int start = pos;
        boolean seenDot = false;
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (Character.isDigit(c)) {
                pos++;
            } else if (c == '.' && !seenDot) {
                seenDot = true;
                pos++;
            } else {
                break;
            }
        }
        return new Token(Kind.NUMBER, src.substring(start, pos), start);
    }

    private Token readIdent() {
        int start = pos;
        while (pos < src.length() && isIdentPart(src.charAt(pos))) {
            pos++;
        }
        return new Token(Kind.IDENT, src.substring(start, pos), start);
    }

    private Token readOperator() {
        int start = pos;
        char c = src.charAt(pos);
        if (c == '=') {
            pos++;
            return new Token(Kind.OP, "=", start);
        }
        if (c == '<') {
            if (pos + 1 < src.length()) {
                char n = src.charAt(pos + 1);
                if (n == '=') {
                    pos += 2;
                    return new Token(Kind.OP, "<=", start);
                }
                if (n == '>') {
                    pos += 2;
                    return new Token(Kind.OP, "<>", start);
                }
            }
            pos++;
            return new Token(Kind.OP, "<", start);
        }
        if (c == '>') {
            if (pos + 1 < src.length() && src.charAt(pos + 1) == '=') {
                pos += 2;
                return new Token(Kind.OP, ">=", start);
            }
            pos++;
            return new Token(Kind.OP, ">", start);
        }
        // c == '!'
        if (pos + 1 < src.length() && src.charAt(pos + 1) == '=') {
            pos += 2;
            return new Token(Kind.OP, "!=", start);
        }
        throw new SqlParseException("非法运算符（位置 " + pos + "，是否想写 <> 或 != ？）");
    }

    private static boolean isIdentStart(char c) {
        return Character.isLetter(c) || c == '_';
    }

    private static boolean isIdentPart(char c) {
        return Character.isLetterOrDigit(c) || c == '_';
    }
}
