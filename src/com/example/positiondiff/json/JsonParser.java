package com.example.positiondiff.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Tiny dependency-free JSON parser.
 *
 * <p>Maps to Java types as: object -> {@link LinkedHashMap}, array ->
 * {@link ArrayList}, string -> String, number -> Double, true/false ->
 * Boolean, null -> null. The parser is strict enough for service input and
 * reports character offsets on malformed input.
 */
public final class JsonParser {

    private final String s;
    private int pos;

    private JsonParser(String s) {
        this.s = s;
    }

    public static Object parse(String input) {
        JsonParser p = new JsonParser(input);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (p.pos != p.s.length()) {
            throw p.error("trailing characters after JSON value");
        }
        return value;
    }

    public static Map<String, Object> parseObject(String input) {
        Object v = parse(input);
        if (!(v instanceof Map<?, ?>)) {
            throw new JsonException("expected JSON object at top level");
        }
        @SuppressWarnings("unchecked")
        Map<String, Object> map = (Map<String, Object>) v;
        return map;
    }

    private Object readValue() {
        skipWs();
        if (pos >= s.length()) throw error("unexpected end of input");
        char c = s.charAt(pos);
        return switch (c) {
            case '{' -> readObject();
            case '[' -> readArray();
            case '"' -> readString();
            case 't', 'f' -> readBoolean();
            case 'n' -> readNull();
            default -> readNumber();
        };
    }

    private Map<String, Object> readObject() {
        Map<String, Object> map = new LinkedHashMap<>();
        expect('{');
        skipWs();
        if (peek() == '}') { pos++; return map; }
        while (true) {
            skipWs();
            if (peek() != '"') throw error("expected string key");
            String key = readString();
            skipWs();
            expect(':');
            Object value = readValue();
            map.put(key, value);
            skipWs();
            char c = next();
            if (c == '}') break;
            if (c != ',') throw error("expected ',' or '}'");
        }
        return map;
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        expect('[');
        skipWs();
        if (peek() == ']') { pos++; return list; }
        while (true) {
            list.add(readValue());
            skipWs();
            char c = next();
            if (c == ']') break;
            if (c != ',') throw error("expected ',' or ']'");
        }
        return list;
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (pos >= s.length()) throw error("unterminated string");
            char c = s.charAt(pos++);
            if (c == '"') break;
            if (c == '\\') {
                if (pos >= s.length()) throw error("unterminated escape");
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
                        if (pos + 4 > s.length()) throw error("bad \\u escape");
                        String hex = s.substring(pos, pos + 4);
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException ex) {
                            throw error("bad \\u escape: " + hex);
                        }
                        pos += 4;
                    }
                    default -> throw error("bad escape \\" + e);
                }
            } else if (c < 0x20) {
                throw error("unescaped control character in string");
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private Boolean readBoolean() {
        if (s.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
        if (s.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
        throw error("invalid literal");
    }

    private Object readNull() {
        if (s.startsWith("null", pos)) { pos += 4; return null; }
        throw error("invalid literal");
    }

    private Double readNumber() {
        int start = pos;
        if (peek() == '-') pos++;
        readDigits();
        if (peek() == '.') {
            pos++;
            readDigits();
        }
        if (peek() == 'e' || peek() == 'E') {
            pos++;
            if (peek() == '+' || peek() == '-') pos++;
            readDigits();
        }
        if (pos == start || (pos == start + 1 && s.charAt(start) == '-')) {
            throw error("invalid number");
        }
        try {
            return Double.valueOf(s.substring(start, pos));
        } catch (NumberFormatException ex) {
            throw error("invalid number");
        }
    }

    private void readDigits() {
        if (pos >= s.length() || !Character.isDigit(s.charAt(pos))) {
            throw error("expected digit");
        }
        while (pos < s.length() && Character.isDigit(s.charAt(pos))) pos++;
    }

    private void skipWs() {
        while (pos < s.length()) {
            char c = s.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
            else break;
        }
    }

    private char peek() {
        if (pos >= s.length()) throw error("unexpected end of input");
        return s.charAt(pos);
    }

    private char next() {
        if (pos >= s.length()) throw error("unexpected end of input");
        return s.charAt(pos++);
    }

    private void expect(char c) {
        if (pos >= s.length() || s.charAt(pos) != c) {
            throw error("expected '" + c + "'");
        }
        pos++;
    }

    private JsonException error(String message) {
        return new JsonException(message + " at offset " + pos);
    }
}
