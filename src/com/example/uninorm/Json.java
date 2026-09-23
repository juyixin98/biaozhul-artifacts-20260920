package com.example.uninorm;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal dependency-free JSON reader/writer, sufficient for the service
 * API. Supports objects, arrays, strings (with {@code \\uXXXX} escapes),
 * numbers, booleans and null. Not a general-purpose JSON library.
 */
public final class Json {

    private Json() {
    }

    // ---------- writing ----------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v) {
        switch (v) {
            case null -> sb.append("null");
            case String s -> writeString(sb, s);
            case Number n -> sb.append(n);
            case Boolean b -> sb.append(b);
            case Map<?, ?> m -> {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<?, ?> e : ((Map<Object, Object>) m).entrySet()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeString(sb, String.valueOf(e.getKey()));
                    sb.append(':');
                    writeValue(sb, e.getValue());
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
                    writeValue(sb, item);
                }
                sb.append(']');
            }
            default -> throw new IllegalArgumentException(
                    "cannot serialize " + v.getClass());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
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

    // ---------- reading ----------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.parseValue();
        p.skipWs();
        if (!p.atEnd()) {
            throw new IllegalArgumentException("trailing characters in JSON");
        }
        return v;
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean atEnd() {
            return pos >= s.length();
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object parseValue() {
            skipWs();
            if (atEnd()) {
                throw new IllegalArgumentException("unexpected end of JSON");
            }
            char c = s.charAt(pos);
            return switch (c) {
                case '{' -> parseObject();
                case '[' -> parseArray();
                case '"' -> parseString();
                case 't' -> {
                    expect("true");
                    yield Boolean.TRUE;
                }
                case 'f' -> {
                    expect("false");
                    yield Boolean.FALSE;
                }
                case 'n' -> {
                    expect("null");
                    yield null;
                }
                default -> parseNumber();
            };
        }

        private void expect(String literal) {
            if (!s.startsWith(literal, pos)) {
                throw new IllegalArgumentException(
                        "expected '" + literal + "' at " + pos);
            }
            pos += literal.length();
        }

        private Map<String, Object> parseObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            pos++; // consume '{'
            skipWs();
            if (!atEnd() && s.charAt(pos) == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWs();
                String key = parseString();
                skipWs();
                if (atEnd() || s.charAt(pos) != ':') {
                    throw new IllegalArgumentException("expected ':' at " + pos);
                }
                pos++;
                Object value = parseValue();
                map.put(key, value);
                skipWs();
                if (atEnd()) {
                    throw new IllegalArgumentException("unterminated object");
                }
                char c = s.charAt(pos);
                if (c == ',') {
                    pos++;
                } else if (c == '}') {
                    pos++;
                    return map;
                } else {
                    throw new IllegalArgumentException(
                            "expected ',' or '}' at " + pos);
                }
            }
        }

        private List<Object> parseArray() {
            List<Object> list = new ArrayList<>();
            pos++; // consume '['
            skipWs();
            if (!atEnd() && s.charAt(pos) == ']') {
                pos++;
                return list;
            }
            while (true) {
                list.add(parseValue());
                skipWs();
                if (atEnd()) {
                    throw new IllegalArgumentException("unterminated array");
                }
                char c = s.charAt(pos);
                if (c == ',') {
                    pos++;
                } else if (c == ']') {
                    pos++;
                    return list;
                } else {
                    throw new IllegalArgumentException(
                            "expected ',' or ']' at " + pos);
                }
            }
        }

        private String parseString() {
            if (atEnd() || s.charAt(pos) != '"') {
                throw new IllegalArgumentException("expected string at " + pos);
            }
            pos++;
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) {
                    throw new IllegalArgumentException("unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (atEnd()) {
                        throw new IllegalArgumentException("bad escape");
                    }
                    char e = s.charAt(pos++);
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'u' -> {
                            if (pos + 4 > s.length()) {
                                throw new IllegalArgumentException(
                                        "bad \\u escape");
                            }
                            sb.append((char) Integer.parseInt(
                                    s.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default -> throw new IllegalArgumentException(
                                "bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        private Number parseNumber() {
            int start = pos;
            if (!atEnd() && s.charAt(pos) == '-') {
                pos++;
            }
            while (!atEnd()) {
                char c = s.charAt(pos);
                if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E'
                        || c == '+' || c == '-') {
                    pos++;
                } else {
                    break;
                }
            }
            if (start == pos) {
                throw new IllegalArgumentException(
                        "unexpected character '" + s.charAt(pos) + "' at " + pos);
            }
            String num = s.substring(start, pos);
            try {
                if (num.contains(".") || num.contains("e") || num.contains("E")) {
                    return Double.parseDouble(num);
                }
                return Long.parseLong(num);
            } catch (NumberFormatException ex) {
                throw new IllegalArgumentException("bad number: " + num);
            }
        }
    }
}
