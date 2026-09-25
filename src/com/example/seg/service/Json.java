package com.example.seg.service;

import java.math.BigDecimal;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小 JSON 实现（零外部依赖）：
 * - 解析支持 object / array / string / number / true / false / null；
 *   数字统一保留为 BigDecimal，避免 long/double 精度损失；
 *   对象用 LinkedHashMap 保留插入顺序。
 * - 序列化支持 Map / List / String / BigDecimal / Number / Boolean / null，
 *   带 2 空格缩进的 pretty 输出，足以覆盖本服务全部响应。
 */
public final class Json {

    // ------------------------------------------------------------------
    // 解析
    // ------------------------------------------------------------------

    public static Object parse(String input) {
        Parser p = new Parser(input);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("JSON 解析失败：结尾有多余字符，位置 " + p.pos);
        }
        return value;
    }

    public static final class JsonException extends RuntimeException {
        public JsonException(String message) {
            super(message);
        }
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return pos >= s.length();
        }

        void skipWs() {
            while (!eof()) {
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
                throw new JsonException("JSON 解析失败：意外结束");
            }
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

        Map<String, Object> readObject() {
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
                    return map;
                }
                if (c != ',') {
                    throw new JsonException("JSON 解析失败：对象里应为 ',' 或 '}'，位置 " + pos);
                }
            }
        }

        List<Object> readArray() {
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
                    return list;
                }
                if (c != ',') {
                    throw new JsonException("JSON 解析失败：数组里应为 ',' 或 ']'，位置 " + pos);
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new JsonException("JSON 解析失败：字符串未闭合");
                }
                char c = next();
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    char esc = next();
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
                            if (pos + 4 > s.length()) {
                                throw new JsonException("JSON 解析失败：\\u 转义不完整");
                            }
                            String hex = s.substring(pos, pos + 4);
                            pos += 4;
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException e) {
                                throw new JsonException("JSON 解析失败：非法 \\u 转义 " + hex);
                            }
                        }
                        default -> throw new JsonException(
                                "JSON 解析失败：非法转义 \\" + esc);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') {
                pos++;
            }
            readDigits();
            if (!eof() && peek() == '.') {
                pos++;
                readDigits();
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                pos++;
                if (!eof() && (peek() == '+' || peek() == '-')) {
                    pos++;
                }
                readDigits();
            }
            String token = s.substring(start, pos);
            try {
                return new BigDecimal(token);
            } catch (NumberFormatException e) {
                throw new JsonException("JSON 解析失败：非法数字 " + token);
            }
        }

        void readDigits() {
            int start = pos;
            while (!eof() && Character.isDigit(peek())) {
                pos++;
            }
            if (start == pos) {
                throw new JsonException("JSON 解析失败：缺少数字，位置 " + pos);
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
            throw new JsonException("JSON 解析失败：非法字面量，位置 " + pos);
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new JsonException("JSON 解析失败：非法字面量，位置 " + pos);
        }

        char peek() {
            if (eof()) {
                throw new JsonException("JSON 解析失败：意外结束");
            }
            return s.charAt(pos);
        }

        char next() {
            char c = peek();
            pos++;
            return c;
        }

        void expect(char expected) {
            skipWs();
            char c = next();
            if (c != expected) {
                throw new JsonException(
                        "JSON 解析失败：期望 '" + expected + "' 实际 '" + c + "'，位置 " + (pos - 1));
            }
        }
    }

    // ------------------------------------------------------------------
    // 序列化（pretty，2 空格缩进）
    // ------------------------------------------------------------------

    public static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object value, int indent) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof Map<?, ?> map) {
            writeObject(sb, map, indent);
        } else if (value instanceof List<?> list) {
            writeArray(sb, list, indent);
        } else if (value instanceof String str) {
            writeString(sb, str);
        } else if (value instanceof BigDecimal bd) {
            sb.append(bd.toPlainString());
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Number n) {
            sb.append(n.toString());
        } else {
            writeString(sb, value.toString());
        }
    }

    private static void writeObject(StringBuilder sb, Map<?, ?> map, int indent) {
        if (map.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append('{');
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append('\n');
            indent(sb, indent + 1);
            writeString(sb, String.valueOf(e.getKey()));
            sb.append(": ");
            writeValue(sb, e.getValue(), indent + 1);
        }
        sb.append('\n');
        indent(sb, indent);
        sb.append('}');
    }

    private static void writeArray(StringBuilder sb, List<?> list, int indent) {
        if (list.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append('[');
        boolean first = true;
        for (Object item : list) {
            if (!first) {
                sb.append(',');
            }
            first = false;
            sb.append('\n');
            indent(sb, indent + 1);
            writeValue(sb, item, indent + 1);
        }
        sb.append('\n');
        indent(sb, indent);
        sb.append(']');
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

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
    }
}
