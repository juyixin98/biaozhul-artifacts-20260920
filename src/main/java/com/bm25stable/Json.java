package com.bm25stable;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖的迷你 JSON 实现：仅支持本项目需要的子集
 * （对象、数组、字符串、数字、true/false/null），字符串支持标准转义与 \\uXXXX。
 * 解析失败抛出 IllegalArgumentException。
 */
public final class Json {

    private Json() {
    }

    // ------------------------------------------------------------------
    // 序列化
    // ------------------------------------------------------------------

    public static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        write(value, sb);
        return sb.toString();
    }

    private static void write(Object value, StringBuilder sb) {
        switch (value) {
            case null -> sb.append("null");
            case String s -> writeString(s, sb);
            case Boolean b -> sb.append(b);
            case Number n -> writeNumber(n, sb);
            case Map<?, ?> map -> {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : map.entrySet()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeString(String.valueOf(e.getKey()), sb);
                    sb.append(':');
                    write(e.getValue(), sb);
                }
                sb.append('}');
            }
            case List<?> list -> {
                sb.append('[');
                boolean first = true;
                for (Object item : list) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    write(item, sb);
                }
                sb.append(']');
            }
            default -> throw new IllegalArgumentException("cannot serialize: " + value.getClass());
        }
    }

    private static void writeNumber(Number n, StringBuilder sb) {
        if (n instanceof Double d) {
            if (d.isNaN() || d.isInfinite()) {
                sb.append("null");
                return;
            }
            sb.append(d);
            return;
        }
        if (n instanceof Float f) {
            if (f.isNaN() || f.isInfinite()) {
                sb.append("null");
                return;
            }
            sb.append(f);
            return;
        }
        sb.append(n);
    }

    private static void writeString(String s, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
                }
            }
        }
        sb.append('"');
    }

    // ------------------------------------------------------------------
    // 解析
    // ------------------------------------------------------------------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWhitespace();
        Object value = p.parseValue();
        p.skipWhitespace();
        if (!p.atEnd()) {
            throw p.error("trailing characters after JSON value");
        }
        return value;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object value = parse(text);
        if (!(value instanceof Map)) {
            throw new IllegalArgumentException("expected a JSON object");
        }
        return (Map<String, Object>) value;
    }

    private static final class Parser {
        private final String text;
        private int pos;

        Parser(String text) {
            this.text = text == null ? "" : text;
        }

        boolean atEnd() {
            return pos >= text.length();
        }

        IllegalArgumentException error(String message) {
            return new IllegalArgumentException(message + " at position " + pos);
        }

        void skipWhitespace() {
            while (!atEnd()) {
                char c = text.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object parseValue() {
            skipWhitespace();
            if (atEnd()) {
                throw error("unexpected end of input");
            }
            char c = text.charAt(pos);
            return switch (c) {
                case '{' -> parseObject();
                case '[' -> parseArray();
                case '"' -> parseString();
                case 't' -> parseLiteral("true", Boolean.TRUE);
                case 'f' -> parseLiteral("false", Boolean.FALSE);
                case 'n' -> parseLiteral("null", null);
                default -> {
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        yield parseNumber();
                    }
                    throw error("unexpected character '" + c + "'");
                }
            };
        }

        private Object parseLiteral(String literal, Object value) {
            if (text.startsWith(literal, pos)) {
                pos += literal.length();
                return value;
            }
            throw error("invalid literal");
        }

        private Map<String, Object> parseObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            pos++; // '{'
            skipWhitespace();
            if (!atEnd() && text.charAt(pos) == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWhitespace();
                if (atEnd() || text.charAt(pos) != '"') {
                    throw error("expected object key string");
                }
                String key = parseString();
                skipWhitespace();
                if (atEnd() || text.charAt(pos) != ':') {
                    throw error("expected ':' after object key");
                }
                pos++;
                map.put(key, parseValue());
                skipWhitespace();
                if (atEnd()) {
                    throw error("unterminated object");
                }
                char c = text.charAt(pos);
                if (c == ',') {
                    pos++;
                } else if (c == '}') {
                    pos++;
                    return map;
                } else {
                    throw error("expected ',' or '}' in object");
                }
            }
        }

        private List<Object> parseArray() {
            List<Object> list = new ArrayList<>();
            pos++; // '['
            skipWhitespace();
            if (!atEnd() && text.charAt(pos) == ']') {
                pos++;
                return list;
            }
            while (true) {
                list.add(parseValue());
                skipWhitespace();
                if (atEnd()) {
                    throw error("unterminated array");
                }
                char c = text.charAt(pos);
                if (c == ',') {
                    pos++;
                } else if (c == ']') {
                    pos++;
                    return list;
                } else {
                    throw error("expected ',' or ']' in array");
                }
            }
        }

        private String parseString() {
            StringBuilder sb = new StringBuilder();
            pos++; // opening quote
            while (!atEnd()) {
                char c = text.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (atEnd()) {
                        throw error("unterminated escape sequence");
                    }
                    char esc = text.charAt(pos++);
                    switch (esc) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'u' -> {
                            if (pos + 4 > text.length()) {
                                throw error("truncated \\u escape");
                            }
                            try {
                                sb.append((char) Integer.parseInt(text.substring(pos, pos + 4), 16));
                            } catch (NumberFormatException e) {
                                throw error("invalid \\u escape");
                            }
                            pos += 4;
                        }
                        default -> throw error("invalid escape character '" + esc + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
            throw error("unterminated string");
        }

        private Number parseNumber() {
            int start = pos;
            if (!atEnd() && text.charAt(pos) == '-') {
                pos++;
            }
            boolean isDouble = false;
            while (!atEnd()) {
                char c = text.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    isDouble = true;
                    pos++;
                } else {
                    break;
                }
            }
            String num = text.substring(start, pos);
            if (num.isEmpty() || "-".equals(num)) {
                throw error("invalid number");
            }
            try {
                if (isDouble) {
                    return Double.parseDouble(num);
                }
                long asLong = Long.parseLong(num);
                if (asLong >= Integer.MIN_VALUE && asLong <= Integer.MAX_VALUE) {
                    return (int) asLong;
                }
                return asLong;
            } catch (NumberFormatException e) {
                throw error("invalid number '" + num + "'");
            }
        }
    }
}
