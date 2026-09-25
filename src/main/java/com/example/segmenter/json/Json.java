package com.example.segmenter.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 零依赖的最小 JSON 解析与序列化（仅支持本服务需要的子集）。
 *
 * <p>Java 类型映射：object={@link LinkedHashMap}（保序），array={@link ArrayList}，
 * string=String，number=Long 或 Double，true/false=Boolean，null=null。
 * 字符串按 UTF-16 转义读写，输出时中文直接以 UTF-8 原样写出。</p>
 */
public final class Json {

    private Json() {
    }

    // ---------- 解析 ----------

    public static Object parse(String input) {
        Parser p = new Parser(input);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("trailing characters at position " + p.pos);
        }
        return value;
    }

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
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

        Object readValue() {
            skipWs();
            if (eof()) {
                throw new JsonException("unexpected end of input");
            }
            char c = s.charAt(pos);
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': case 'f': return readBoolean();
                case 'n': return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return readNumber();
                    }
                    throw new JsonException("unexpected character '" + c + "' at position " + pos);
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
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
                    return map;
                }
                if (c != ',') {
                    throw new JsonException("expected ',' or '}' at position " + (pos - 1));
                }
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
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
                    return list;
                }
                if (c != ',') {
                    throw new JsonException("expected ',' or ']' at position " + (pos - 1));
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new JsonException("unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (eof()) {
                        throw new JsonException("unterminated escape");
                    }
                    char e = s.charAt(pos++);
                    switch (e) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u':
                            if (pos + 4 > s.length()) {
                                throw new JsonException("bad \\u escape");
                            }
                            String hex = s.substring(pos, pos + 4);
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException nfe) {
                                throw new JsonException("bad \\u escape: " + hex);
                            }
                            pos += 4;
                            break;
                        default:
                            throw new JsonException("invalid escape \\" + e);
                    }
                } else {
                    if (c < 0x20) {
                        throw new JsonException("unescaped control character in string");
                    }
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (s.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new JsonException("invalid literal at position " + pos);
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new JsonException("invalid literal at position " + pos);
        }

        Number readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            readDigits();
            boolean isDouble = false;
            if (!eof() && s.charAt(pos) == '.') {
                isDouble = true;
                pos++;
                readDigits();
            }
            if (!eof() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                isDouble = true;
                pos++;
                if (!eof() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) pos++;
                readDigits();
            }
            String token = s.substring(start, pos);
            if (isDouble) {
                double d = Double.parseDouble(token);
                if (!Double.isFinite(d)) {
                    throw new JsonException("non-finite number: " + token);
                }
                return d;
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException nfe) {
                throw new JsonException("invalid number: " + token);
            }
        }

        void readDigits() {
            int begin = pos;
            while (pos < s.length() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
            if (pos == begin) {
                throw new JsonException("expected digits at position " + pos);
            }
        }

        char peek() {
            if (eof()) {
                throw new JsonException("unexpected end of input");
            }
            return s.charAt(pos);
        }

        char next() {
            if (eof()) {
                throw new JsonException("unexpected end of input");
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            if (eof() || s.charAt(pos) != c) {
                throw new JsonException("expected '" + c + "' at position " + pos);
            }
            pos++;
        }
    }

    // ---------- 序列化 ----------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String writePretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writeIndented(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String) {
            writeString(sb, (String) value);
        } else if (value instanceof Boolean) {
            sb.append(value.toString());
        } else if (value instanceof Integer || value instanceof Long) {
            sb.append(value.toString());
        } else if (value instanceof Double || value instanceof Float) {
            double d = ((Number) value).doubleValue();
            if (!Double.isFinite(d)) {
                throw new JsonException("non-finite number cannot be serialized");
            }
            sb.append(doubleToString(d));
        } else if (value instanceof Number) {
            sb.append(value.toString());
        } else if (value instanceof Map) {
            writeObject(sb, (Map<String, Object>) value);
        } else if (value instanceof List) {
            writeArray(sb, (List<Object>) value);
        } else {
            throw new JsonException("unsupported type: " + value.getClass());
        }
    }

    private static void writeObject(StringBuilder sb, Map<String, Object> map) {
        sb.append('{');
        boolean first = true;
        for (Map.Entry<String, Object> e : map.entrySet()) {
            if (!first) sb.append(',');
            first = false;
            writeString(sb, e.getKey());
            sb.append(':');
            writeValue(sb, e.getValue());
        }
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<Object> list) {
        sb.append('[');
        boolean first = true;
        for (Object item : list) {
            if (!first) sb.append(',');
            first = false;
            writeValue(sb, item);
        }
        sb.append(']');
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    /** 避免 Double.toString 产生科学计数法以外的不一致；保留至多 6 位小数（服务层已先舍入）。 */
    static String doubleToString(double d) {
        if (d == Math.floor(d) && !Double.isInfinite(d) && Math.abs(d) < 1e16) {
            return Long.toString((long) d);
        }
        return Double.toString(d);
    }

    @SuppressWarnings("unchecked")
    private static void writeIndented(StringBuilder sb, Object value, int indent) {
        if (value instanceof Map) {
            Map<String, Object> map = (Map<String, Object>) value;
            if (map.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<String, Object> e : map.entrySet()) {
                if (!first) sb.append(",\n");
                first = false;
                indent(sb, indent + 1);
                writeString(sb, e.getKey());
                sb.append(": ");
                writeIndented(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (value instanceof List) {
            List<Object> list = (List<Object>) value;
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append("[\n");
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append(",\n");
                indent(sb, indent + 1);
                writeIndented(sb, list.get(i), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            writeValue(sb, value);
        }
    }

    private static void indent(StringBuilder sb, int level) {
        for (int i = 0; i < level; i++) {
            sb.append("  ");
        }
    }
}
