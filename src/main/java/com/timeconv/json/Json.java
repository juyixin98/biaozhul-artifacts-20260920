package com.timeconv.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal JSON reader/writer with no external dependencies.
 *
 * <p>Numbers are preserved as {@link JsonNumber} raw text so that 64-bit and larger
 * integers survive parsing exactly — nothing ever passes through a double.
 *
 * <p>Java representation: {@code Map<String,Object>} / {@code List<Object>} /
 * {@code String} / {@code JsonNumber} / {@code Boolean} / {@code null}.
 */
public final class Json {

    private Json() {
    }

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWhitespace();
        Object v = p.parseValue();
        p.skipWhitespace();
        if (!p.atEnd()) {
            throw p.error("trailing characters after JSON value");
        }
        return v;
    }

    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonParseException("expected a JSON object");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> m = (Map<String, Object>) v;
        return m;
    }

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof JsonNumber n) {
            sb.append(n.raw());
        } else if (v instanceof Boolean b) {
            sb.append(b);
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List<?> list) {
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
        } else {
            throw new IllegalArgumentException("cannot write JSON for: " + v.getClass());
        }
    }

    private static void writeString(StringBuilder sb, String s) {
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

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s == null ? "" : s;
        }

        boolean atEnd() {
            return pos >= s.length();
        }

        void skipWhitespace() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        JsonParseException error(String msg) {
            return new JsonParseException(msg + " at position " + pos);
        }

        Object parseValue() {
            if (atEnd()) {
                throw error("unexpected end of input");
            }
            char c = s.charAt(pos);
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

        private Map<String, Object> parseObject() {
            pos++; // consume '{'
            Map<String, Object> map = new LinkedHashMap<>();
            skipWhitespace();
            if (!atEnd() && s.charAt(pos) == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWhitespace();
                if (atEnd() || s.charAt(pos) != '"') {
                    throw error("expected object key string");
                }
                String key = parseString();
                skipWhitespace();
                if (atEnd() || s.charAt(pos) != ':') {
                    throw error("expected ':'");
                }
                pos++;
                skipWhitespace();
                map.put(key, parseValue());
                skipWhitespace();
                if (atEnd()) {
                    throw error("unterminated object");
                }
                char c = s.charAt(pos);
                if (c == ',') {
                    pos++;
                } else if (c == '}') {
                    pos++;
                    return map;
                } else {
                    throw error("expected ',' or '}'");
                }
            }
        }

        private List<Object> parseArray() {
            pos++; // consume '['
            List<Object> list = new ArrayList<>();
            skipWhitespace();
            if (!atEnd() && s.charAt(pos) == ']') {
                pos++;
                return list;
            }
            while (true) {
                skipWhitespace();
                list.add(parseValue());
                skipWhitespace();
                if (atEnd()) {
                    throw error("unterminated array");
                }
                char c = s.charAt(pos);
                if (c == ',') {
                    pos++;
                } else if (c == ']') {
                    pos++;
                    return list;
                } else {
                    throw error("expected ',' or ']'");
                }
            }
        }

        private String parseString() {
            pos++; // consume opening quote
            StringBuilder sb = new StringBuilder();
            while (!atEnd()) {
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (atEnd()) {
                        throw error("unterminated escape");
                    }
                    char e = s.charAt(pos++);
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'u' -> {
                            if (pos + 4 > s.length()) {
                                throw error("truncated \\u escape");
                            }
                            try {
                                sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            } catch (NumberFormatException ex) {
                                throw error("invalid \\u escape");
                            }
                            pos += 4;
                        }
                        default -> throw error("invalid escape '\\" + e + "'");
                    }
                } else {
                    sb.append(c);
                }
            }
            throw error("unterminated string");
        }

        private Object parseLiteral(String literal, Object value) {
            if (s.startsWith(literal, pos)) {
                pos += literal.length();
                return value;
            }
            throw error("invalid literal");
        }

        private JsonNumber parseNumber() {
            int start = pos;
            if (!atEnd() && s.charAt(pos) == '-') {
                pos++;
            }
            readDigits();
            if (!atEnd() && s.charAt(pos) == '.') {
                pos++;
                readDigits();
            }
            if (!atEnd() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                pos++;
                if (!atEnd() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) {
                    pos++;
                }
                readDigits();
            }
            String raw = s.substring(start, pos);
            if (raw.equals("-") || raw.isEmpty()) {
                throw error("invalid number");
            }
            return new JsonNumber(raw);
        }

        private void readDigits() {
            while (!atEnd() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
        }
    }
}
