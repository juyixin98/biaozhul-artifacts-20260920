package com.example.tvl.sql;

import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * SQL 子集的词法分析器。关键字大小写不敏感，标识符保留原始拼写
 * （列名匹配在解析阶段做大小写不敏感处理）。
 */
final class Tokenizer {

    record Token(String kind, String text, Object value, int pos) {
        boolean isKeyword(String kw) {
            return kind.equals("KEYWORD") && text.equalsIgnoreCase(kw);
        }
    }

    private static final Map<String, String> KEYWORDS = Map.of(
            "SELECT", "SELECT", "FROM", "FROM", "WHERE", "WHERE",
            "AND", "AND", "OR", "OR", "NOT", "NOT", "IS", "IS",
            "NULL", "NULL", "TRUE", "TRUE", "FALSE", "FALSE"
    );

    private final String src;
    private int pos;

    Tokenizer(String src) {
        this.src = src;
        this.pos = 0;
    }

    List<Token> tokenize() {
        List<Token> tokens = new ArrayList<>();
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (Character.isWhitespace(c)) {
                pos++;
            } else if (c == '-' && pos + 1 < src.length() && isNumberStart(src.charAt(pos + 1))) {
                tokens.add(readNumber());
            } else if (isNumberStart(c)) {
                tokens.add(readNumber());
            } else if (Character.isLetter(c) || c == '_') {
                tokens.add(readWord());
            } else if (c == '\'') {
                tokens.add(readString());
            } else {
                tokens.add(readSymbol());
            }
        }
        tokens.add(new Token("EOF", "", null, pos));
        return tokens;
    }

    private static boolean isNumberStart(char c) {
        return c >= '0' && c <= '9';
    }

    private Token readNumber() {
        int start = pos;
        if (src.charAt(pos) == '-') {
            pos++;
        }
        boolean seenDot = false;
        boolean seenExp = false;
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (isNumberStart(c)) {
                pos++;
            } else if (c == '.' && !seenDot && !seenExp) {
                seenDot = true;
                pos++;
            } else if ((c == 'e' || c == 'E') && !seenExp) {
                seenExp = true;
                pos++;
                if (pos < src.length() && (src.charAt(pos) == '+' || src.charAt(pos) == '-')) {
                    pos++;
                }
            } else {
                break;
            }
        }
        String text = src.substring(start, pos);
        Object value;
        if (seenDot || seenExp) {
            value = Double.valueOf(Double.parseDouble(text));
        } else {
            try {
                value = Long.parseLong(text);
            } catch (NumberFormatException e) {
                // 超出 long 范围的整型字面量按浮点处理
                value = Double.valueOf(Double.parseDouble(text));
            }
        }
        return new Token("NUMBER", text, value, start);
    }

    private Token readWord() {
        int start = pos;
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (Character.isLetterOrDigit(c) || c == '_') {
                pos++;
            } else {
                break;
            }
        }
        String word = src.substring(start, pos);
        String upper = word.toUpperCase();
        if (KEYWORDS.containsKey(upper)) {
            if (upper.equals("TRUE") || upper.equals("FALSE")) {
                // 本子集不支持布尔字面量（列没有布尔类型），交由解析器报错
                throw new SqlParseException("不支持布尔字面量 " + word + "，位置 " + start);
            }
            return new Token("KEYWORD", upper, null, start);
        }
        return new Token("IDENT", word, null, start);
    }

    private Token readString() {
        int start = pos;
        pos++; // 跳过开头的引号
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= src.length()) {
                throw new SqlParseException("字符串未闭合，位置 " + start);
            }
            char c = src.charAt(pos);
            if (c == '\'') {
                if (pos + 1 < src.length() && src.charAt(pos + 1) == '\'') {
                    sb.append('\'');
                    pos += 2;
                } else {
                    pos++;
                    break;
                }
            } else {
                sb.append(c);
                pos++;
            }
        }
        return new Token("STRING", src.substring(start, pos), sb.toString(), start);
    }

    private Token readSymbol() {
        int start = pos;
        char c = src.charAt(pos);
        switch (c) {
            case '(', ')', ',', '*', '?', ';' -> {
                pos++;
                return new Token("SYMBOL", String.valueOf(c), null, start);
            }
            case '=' -> {
                pos++;
                if (pos < src.length() && src.charAt(pos) == '=') {
                    pos++; // 容忍 "=="
                }
                return new Token("SYMBOL", "=", null, start);
            }
            case '<' -> {
                pos++;
                if (pos < src.length()) {
                    char n = src.charAt(pos);
                    if (n == '=') {
                        pos++;
                        return new Token("SYMBOL", "<=", null, start);
                    }
                    if (n == '>') {
                        pos++;
                        return new Token("SYMBOL", "<>", null, start);
                    }
                }
                return new Token("SYMBOL", "<", null, start);
            }
            case '>' -> {
                pos++;
                if (pos < src.length() && src.charAt(pos) == '=') {
                    pos++;
                    return new Token("SYMBOL", ">=", null, start);
                }
                return new Token("SYMBOL", ">", null, start);
            }
            case '!' -> {
                if (pos + 1 < src.length() && src.charAt(pos + 1) == '=') {
                    pos += 2;
                    return new Token("SYMBOL", "<>", null, start);
                }
                throw new SqlParseException("意外字符 '!'，位置 " + start);
            }
            default -> throw new SqlParseException("意外字符 '" + c + "'，位置 " + start);
        }
    }
}
