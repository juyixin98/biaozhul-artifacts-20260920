package com.example.iview.json;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal self-contained JSON parser / pretty writer.
 * Uses only the JDK. Number mapping:
 *  - integer tokens -> Long
 *  - decimal/exponent tokens -> BigDecimal
 */
public final class Json {

    private Json() {
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String input) {
        Object v = parse(input);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("Expected a JSON object");
        }
        return (Map<String, Object>) v;
    }

    public static Object parse(String input) {
        Parser p = new Parser(input);
        p.skipWhitespace();
        Object value = p.readValue();
        p.skipWhitespace();
        if (!p.eof()) {
            throw new IllegalArgumentException("Trailing characters at index " + p.i);
        }
        return value;
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void write(StringBuilder sb, Object value, int indent) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(sb, s);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Long l) {
            sb.append(l);
        } else if (value instanceof Integer i) {
            sb.append(i);
        } else if (value instanceof BigDecimal bd) {
            sb.append(bd.toPlainString());
        } else if (value instanceof Map<?, ?> map) {
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                write(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (value instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                indent(sb, indent + 1);
                write(sb, item, indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            throw new IllegalArgumentException("Cannot serialize value of type " + value.getClass());
        }
    }

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
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

    private static final class Parser {
        private final String s;
        private int i;

        private Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return i >= s.length();
        }

        void skipWhitespace() {
            while (!eof() && Character.isWhitespace(s.charAt(i))) {
                i++;
            }
        }

        void expect(char c) {
            if (eof() || s.charAt(i) != c) {
                throw new IllegalArgumentException("Expected '" + c + "' at index " + i);
            }
            i++;
        }

        Object readValue() {
            skipWhitespace();
            if (eof()) {
                throw new IllegalArgumentException("Unexpected end of JSON");
            }
            char c = s.charAt(i);
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> readNumber();
            };
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWhitespace();
            if (!eof() && s.charAt(i) == '}') {
                i++;
                return map;
            }
            while (true) {
                skipWhitespace();
                String key = readString();
                skipWhitespace();
                expect(':');
                Object value = readValue();
                map.put(key, value);
                skipWhitespace();
                char c = s.charAt(i);
                if (c == ',') {
                    i++;
                    continue;
                }
                if (c == '}') {
                    i++;
                    break;
                }
                throw new IllegalArgumentException("Expected ',' or '}' at index " + i);
            }
            return map;
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWhitespace();
            if (!eof() && s.charAt(i) == ']') {
                i++;
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWhitespace();
                char c = s.charAt(i);
                if (c == ',') {
                    i++;
                    continue;
                }
                if (c == ']') {
                    i++;
                    break;
                }
                throw new IllegalArgumentException("Expected ',' or ']' at index " + i);
            }
            return list;
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new IllegalArgumentException("Unterminated string");
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    break;
                }
                if (c == '\\') {
                    if (eof()) {
                        throw new IllegalArgumentException("Unterminated escape");
                    }
                    char e = s.charAt(i++);
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
                            if (i + 4 > s.length()) {
                                throw new IllegalArgumentException("Bad unicode escape");
                            }
                            sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            i += 4;
                        }
                        default -> throw new IllegalArgumentException("Bad escape: \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        Boolean readBoolean() {
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            throw new IllegalArgumentException("Invalid literal at index " + i);
        }

        Object readNull() {
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            throw new IllegalArgumentException("Invalid literal at index " + i);
        }

        Object readNumber() {
            int start = i;
            while (!eof()) {
                char c = s.charAt(i);
                if (c == ',' || c == '}' || c == ']' || Character.isWhitespace(c)) {
                    break;
                }
                i++;
            }
            String token = s.substring(start, i);
            if (token.isEmpty()) {
                throw new IllegalArgumentException("Invalid number at index " + start);
            }
            boolean integral = token.chars().noneMatch(ch -> ch == '.' || ch == 'e' || ch == 'E');
            if (integral) {
                try {
                    return Long.parseLong(token);
                } catch (NumberFormatException ignore) {
                    // fall through to BigDecimal
                }
            }
            try {
                return new BigDecimal(token);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("Invalid number '" + token + "'");
            }
        }
    }
}
