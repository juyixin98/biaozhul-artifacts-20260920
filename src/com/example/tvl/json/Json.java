package com.example.tvl.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析器（无第三方依赖）。
 * 支持 object/array/string/number/true/false/null。
 * 数字统一解析：无小数点与指数时为 Long，溢出或浮点时为 Double。
 */
public final class Json {

    private final String src;
    private int pos;

    private Json(String src) {
        this.src = src;
        this.pos = 0;
    }

    public static Object parse(String text) {
        if (text == null || text.isBlank()) {
            throw new JsonException("请求体为空");
        }
        Json p = new Json(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != p.src.length()) {
            throw new JsonException("JSON 解析后存在多余字符，位置 " + p.pos);
        }
        return v;
    }

    private Object readValue() {
        skipWs();
        if (pos >= src.length()) {
            throw new JsonException("意外结束");
        }
        char c = src.charAt(pos);
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
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWs();
            String key = readString();
            skipWs();
            expect(':');
            Object value = readValue();
            map.put(key, value);
            skipWs();
            char c = next();
            if (c == '}') {
                break;
            }
            if (c != ',') {
                throw new JsonException("期望 ',' 或 '}'，位置 " + (pos - 1));
            }
        }
        return map;
    }

    private List<Object> readArray() {
        List<Object> list = new ArrayList<>();
        expect('[');
        skipWs();
        if (peek() == ']') {
            pos++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWs();
            char c = next();
            if (c == ']') {
                break;
            }
            if (c != ',') {
                throw new JsonException("期望 ',' 或 ']'，位置 " + (pos - 1));
            }
        }
        return list;
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            char c = next();
            if (c == '"') {
                break;
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
                        if (pos + 4 > src.length()) {
                            throw new JsonException("\\u 转义不完整");
                        }
                        sb.append((char) Integer.parseInt(src.substring(pos, pos + 4), 16));
                        pos += 4;
                    }
                    default -> throw new JsonException("非法转义 \\" + e);
                }
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private Boolean readBoolean() {
        if (src.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (src.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw new JsonException("非法字面量，位置 " + pos);
    }

    private Object readNull() {
        if (src.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw new JsonException("非法字面量，位置 " + pos);
    }

    private Object readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
        }
        boolean isFloat = false;
        while (pos < src.length()) {
            char c = src.charAt(pos);
            if (c >= '0' && c <= '9') {
                pos++;
            } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                isFloat = true;
                pos++;
            } else {
                break;
            }
        }
        String text = src.substring(start, pos);
        if (text.isEmpty() || text.equals("-")) {
            throw new JsonException("非法数字，位置 " + start);
        }
        try {
            if (isFloat) {
                return Double.valueOf(Double.parseDouble(text));
            }
            return Long.parseLong(text);
        } catch (NumberFormatException e) {
            try {
                return Double.valueOf(Double.parseDouble(text));
            } catch (NumberFormatException e2) {
                throw new JsonException("非法数字 '" + text + "'");
            }
        }
    }

    private char peek() {
        return pos >= src.length() ? '\0' : src.charAt(pos);
    }

    private char next() {
        if (pos >= src.length()) {
            throw new JsonException("意外结束");
        }
        return src.charAt(pos++);
    }

    private void expect(char c) {
        if (peek() != c) {
            throw new JsonException("期望 '" + c + "'，位置 " + pos);
        }
        pos++;
    }

    private void skipWs() {
        while (pos < src.length() && Character.isWhitespace(src.charAt(pos))) {
            pos++;
        }
    }

    // ---- 序列化 ----

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writeIndented(sb, value, 0);
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Long l) {
            sb.append(l);
        } else if (v instanceof Integer i) {
            sb.append(i);
        } else if (v instanceof Double d) {
            sb.append(d.isNaN() || d.isInfinite() ? "null" : d);
        } else if (v instanceof Number n) {
            sb.append(n);
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
            for (Object o : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, o);
            }
            sb.append(']');
        } else {
            throw new JsonException("无法序列化类型: " + v.getClass());
        }
    }

    private static void writeIndented(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map<?, ?> m) {
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            String pad = "  ".repeat(indent + 1);
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                sb.append(pad);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writeIndented(sb, e.getValue(), indent + 1);
            }
            sb.append('\n').append("  ".repeat(indent)).append('}');
        } else if (v instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            String pad = "  ".repeat(indent + 1);
            boolean first = true;
            for (Object o : list) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                sb.append(pad);
                writeIndented(sb, o, indent + 1);
            }
            sb.append('\n').append("  ".repeat(indent)).append(']');
        } else {
            writeValue(sb, v);
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
}
