package com.example.ac.server;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简手写 JSON 解析器（零外部依赖）。
 *
 * <p>支持 object / array / string / number / true / false / null。
 * 数字统一解析为 Long 或 Double；object 保持插入顺序（LinkedHashMap）。
 */
public final class JsonParser {

    private final String s;
    private int i;

    private JsonParser(String s) {
        this.s = s;
    }

    public static Object parse(String text) {
        JsonParser p = new JsonParser(text);
        p.skipWs();
        Object v = p.value();
        p.skipWs();
        if (p.i != p.s.length()) {
            throw new JsonException("trailing characters at position " + p.i);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    private Object value() {
        skipWs();
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        char c = s.charAt(i);
        return switch (c) {
            case '{' -> object();
            case '[' -> array();
            case '"' -> string();
            case 't', 'f' -> bool();
            case 'n' -> nul();
            default -> number();
        };
    }

    private Map<String, Object> object() {
        expect('{');
        Map<String, Object> map = new LinkedHashMap<>();
        skipWs();
        if (peek() == '}') {
            i++;
            return map;
        }
        while (true) {
            skipWs();
            String key = string();
            skipWs();
            expect(':');
            map.put(key, value());
            skipWs();
            char c = next();
            if (c == '}') {
                return map;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or '}' at position " + (i - 1));
            }
        }
    }

    private List<Object> array() {
        expect('[');
        List<Object> list = new ArrayList<>();
        skipWs();
        if (peek() == ']') {
            i++;
            return list;
        }
        while (true) {
            list.add(value());
            skipWs();
            char c = next();
            if (c == ']') {
                return list;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or ']' at position " + (i - 1));
            }
        }
    }

    private String string() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (i >= s.length()) {
                throw new JsonException("unterminated string");
            }
            char c = s.charAt(i++);
            switch (c) {
                case '"' -> {
                    return sb.toString();
                }
                case '\\' -> {
                    if (i >= s.length()) {
                        throw new JsonException("unterminated escape");
                    }
                    char e = s.charAt(i++);
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
                            if (i + 4 > s.length()) {
                                throw new JsonException("bad \\u escape");
                            }
                            int cp = Integer.parseInt(s.substring(i, i + 4), 16);
                            sb.append((char) cp);
                            i += 4;
                        }
                        default -> throw new JsonException("bad escape: \\" + e);
                    }
                }
                default -> {
                    if (c < 0x20) {
                        throw new JsonException("unescaped control character in string");
                    }
                    sb.append(c);
                }
            }
        }
    }

    private Object number() {
        int start = i;
        if (peek() == '-') {
            i++;
        }
        boolean isDouble = false;
        while (i < s.length()) {
            char c = s.charAt(i);
            if ((c >= '0' && c <= '9')) {
                i++;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                isDouble = true;
                i++;
            } else {
                break;
            }
        }
        String token = s.substring(start, i);
        if (token.isEmpty() || token.equals("-")) {
            throw new JsonException("invalid number at position " + start);
        }
        try {
            return isDouble ? Double.parseDouble(token) : Long.parseLong(token);
        } catch (NumberFormatException ex) {
            throw new JsonException("invalid number: " + token);
        }
    }

    private Boolean bool() {
        if (s.startsWith("true", i)) {
            i += 4;
            return Boolean.TRUE;
        }
        if (s.startsWith("false", i)) {
            i += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("invalid literal at position " + i);
    }

    private Object nul() {
        if (s.startsWith("null", i)) {
            i += 4;
            return null;
        }
        throw new JsonException("invalid literal at position " + i);
    }

    private void expect(char c) {
        if (i >= s.length() || s.charAt(i) != c) {
            throw new JsonException("expected '" + c + "' at position " + i);
        }
        i++;
    }

    private char next() {
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        return s.charAt(i++);
    }

    private char peek() {
        return i >= s.length() ? '\0' : s.charAt(i);
    }

    private void skipWs() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }
}
