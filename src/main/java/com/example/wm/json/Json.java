package com.example.wm.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简递归下降 JSON 解析器（仅依赖 JDK）。
 * 支持 object / array / string / number / true / false / null，
 * 数字统一解析为 Long（无小数/指数）或 Double。
 */
public final class Json {

    private final String s;
    private int i;

    private Json(String s) {
        this.s = s;
    }

    /** 把 JSON 文本解析为 Map / List / String / Long / Double / Boolean / null。 */
    public static Object parse(String text) {
        Json p = new Json(text);
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
        Map<String, Object> m = new LinkedHashMap<>();
        expect('{');
        skipWs();
        if (peek() == '}') {
            i++;
            return m;
        }
        while (true) {
            skipWs();
            String key = string();
            skipWs();
            expect(':');
            Object val = value();
            m.put(key, val);
            skipWs();
            char c = next();
            if (c == '}') {
                return m;
            }
            if (c != ',') {
                throw new JsonException("expected ',' or '}' at position " + (i - 1));
            }
        }
    }

    private List<Object> array() {
        List<Object> list = new ArrayList<>();
        expect('[');
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
            char c = next();
            if (c == '"') {
                return sb.toString();
            }
            if (c == '\\') {
                char e = next();
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
                        int hex = Integer.parseInt(s.substring(i, i + 4), 16);
                        i += 4;
                        sb.append((char) hex);
                    }
                    default -> throw new JsonException("bad escape \\" + e);
                }
            } else {
                if (c < 0x20) {
                    throw new JsonException("unescaped control character in string");
                }
                sb.append(c);
            }
        }
    }

    private Object number() {
        int start = i;
        if (peek() == '-') {
            i++;
        }
        while (i < s.length() && Character.isDigit(s.charAt(i))) {
            i++;
        }
        boolean isDouble = false;
        if (i < s.length() && s.charAt(i) == '.') {
            isDouble = true;
            i++;
            while (i < s.length() && Character.isDigit(s.charAt(i))) {
                i++;
            }
        }
        if (i < s.length() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
            int afterE = i + 1;
            if (afterE < s.length() && (s.charAt(afterE) == '+' || s.charAt(afterE) == '-')) {
                afterE++;
            }
            int digitStart = afterE;
            while (afterE < s.length() && Character.isDigit(s.charAt(afterE))) {
                afterE++;
            }
            if (afterE == digitStart) {
                throw new JsonException("invalid number exponent at position " + i);
            }
            // 形如 1e3（指数非负）仍是整数值，解析为 Long；含小数点或负指数才用 Double
            boolean negativeExponent = s.charAt(i + 1) == '-';
            if (isDouble || negativeExponent) {
                isDouble = true;
            }
            i = afterE;
        }
        String token = s.substring(start, i);
        if (token.isEmpty() || token.equals("-")) {
            throw new JsonException("invalid number at position " + start);
        }
        if (isDouble) {
            return Double.parseDouble(token);
        }
        try {
            return Long.parseLong(token);
        } catch (NumberFormatException e) {
            // 超出 long 范围的整数退化为 double
            return Double.parseDouble(token);
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

    private char peek() {
        return i >= s.length() ? '\0' : s.charAt(i);
    }

    private char next() {
        if (i >= s.length()) {
            throw new JsonException("unexpected end of input");
        }
        return s.charAt(i++);
    }

    private void expect(char c) {
        if (next() != c) {
            throw new JsonException("expected '" + c + "' at position " + (i - 1));
        }
    }

    private void skipWs() {
        while (i < s.length() && Character.isWhitespace(s.charAt(i))) {
            i++;
        }
    }
}
