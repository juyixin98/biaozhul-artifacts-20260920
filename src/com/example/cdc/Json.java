package com.example.cdc;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析器与序列化器，仅使用 JDK 标准库。
 * 解析结果类型：{@link LinkedHashMap}、{@link ArrayList}、String、Long、Double、Boolean、null。
 */
public final class Json {

    private Json() {
    }

    // ---------- 解析 ----------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw p.error("JSON 尾部存在多余字符");
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new IllegalArgumentException("请求体必须是 JSON 对象");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int i;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return i >= s.length();
        }

        IllegalArgumentException error(String msg) {
            return new IllegalArgumentException("JSON 解析失败(位置 " + i + "): " + msg);
        }

        void skipWs() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    i++;
                } else {
                    break;
                }
            }
        }

        Object readValue() {
            skipWs();
            if (eof()) {
                throw error("意外结束");
            }
            char c = s.charAt(i);
            switch (c) {
                case '{':
                    return readObject();
                case '[':
                    return readArray();
                case '"':
                    return readString();
                case 't':
                case 'f':
                    return readBool();
                case 'n':
                    return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        return readNumber();
                    }
                    throw error("非法字符 '" + c + "'");
            }
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            i++; // {
            skipWs();
            if (!eof() && s.charAt(i) == '}') {
                i++;
                return map;
            }
            while (true) {
                skipWs();
                if (eof() || s.charAt(i) != '"') {
                    throw error("对象键必须是字符串");
                }
                String key = readString();
                skipWs();
                if (eof() || s.charAt(i) != ':') {
                    throw error("对象中缺少 ':'");
                }
                i++;
                Object value = readValue();
                map.put(key, value);
                skipWs();
                if (eof()) {
                    throw error("对象未闭合");
                }
                char c = s.charAt(i);
                if (c == ',') {
                    i++;
                } else if (c == '}') {
                    i++;
                    return map;
                } else {
                    throw error("对象中应为 ',' 或 '}'，实际为 '" + c + "'");
                }
            }
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            i++; // [
            skipWs();
            if (!eof() && s.charAt(i) == ']') {
                i++;
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWs();
                if (eof()) {
                    throw error("数组未闭合");
                }
                char c = s.charAt(i);
                if (c == ',') {
                    i++;
                } else if (c == ']') {
                    i++;
                    return list;
                } else {
                    throw error("数组中应为 ',' 或 ']'，实际为 '" + c + "'");
                }
            }
        }

        String readString() {
            i++; // 引号
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw error("字符串未闭合");
                }
                char c = s.charAt(i++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (eof()) {
                        throw error("非法转义");
                    }
                    char e = s.charAt(i++);
                    switch (e) {
                        case '"':
                            sb.append('"');
                            break;
                        case '\\':
                            sb.append('\\');
                            break;
                        case '/':
                            sb.append('/');
                            break;
                        case 'b':
                            sb.append('\b');
                            break;
                        case 'f':
                            sb.append('\f');
                            break;
                        case 'n':
                            sb.append('\n');
                            break;
                        case 'r':
                            sb.append('\r');
                            break;
                        case 't':
                            sb.append('\t');
                            break;
                        case 'u':
                            if (i + 4 > s.length()) {
                                throw error("\\u 转义不完整");
                            }
                            sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            i += 4;
                            break;
                        default:
                            throw error("非法转义 \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBool() {
            if (s.startsWith("true", i)) {
                i += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", i)) {
                i += 5;
                return Boolean.FALSE;
            }
            throw error("非法字面量");
        }

        Object readNull() {
            if (s.startsWith("null", i)) {
                i += 4;
                return null;
            }
            throw error("非法字面量");
        }

        Object readNumber() {
            int start = i;
            if (s.charAt(i) == '-') {
                i++;
            }
            readDigits();
            boolean isDouble = false;
            if (!eof() && s.charAt(i) == '.') {
                isDouble = true;
                i++;
                readDigits();
            }
            if (!eof() && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
                isDouble = true;
                i++;
                if (!eof() && (s.charAt(i) == '+' || s.charAt(i) == '-')) {
                    i++;
                }
                readDigits();
            }
            String num = s.substring(start, i);
            if (isDouble) {
                return Double.valueOf(num);
            }
            return Long.valueOf(num);
        }

        void readDigits() {
            if (eof() || !Character.isDigit(s.charAt(i))) {
                throw error("数字格式错误");
            }
            while (!eof() && Character.isDigit(s.charAt(i))) {
                i++;
            }
        }
    }

    // ---------- 序列化 ----------

    /** 紧凑序列化（WAL、规范化键等场景）。 */
    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, v, 0, 0);
        return sb.toString();
    }

    /** 带两空格缩进的序列化（HTTP 响应）。 */
    public static String writePretty(Object v) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, v, 0, 2);
        sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v, int depth, int indent) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v).booleanValue());
        } else if (v instanceof Number || v instanceof Enum) {
            sb.append(v);
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Map) {
            Map<String, Object> m = (Map<String, Object>) v;
            if (m.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : m.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                newline(sb, depth + 1, indent);
                writeString(sb, e.getKey());
                sb.append(indent == 0 ? ":" : ": ");
                writeValue(sb, e.getValue(), depth + 1, indent);
            }
            newline(sb, depth, indent);
            sb.append('}');
        } else if (v instanceof List) {
            List<Object> l = (List<Object>) v;
            if (l.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append('[');
            for (int idx = 0; idx < l.size(); idx++) {
                if (idx > 0) {
                    sb.append(',');
                }
                newline(sb, depth + 1, indent);
                writeValue(sb, l.get(idx), depth + 1, indent);
            }
            newline(sb, depth, indent);
            sb.append(']');
        } else {
            throw new IllegalArgumentException("无法序列化类型: " + v.getClass());
        }
    }

    private static void newline(StringBuilder sb, int depth, int indent) {
        if (indent == 0) {
            return;
        }
        sb.append('\n');
        sb.append("  ".repeat(Math.max(0, depth)));
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':
                    sb.append("\\\"");
                    break;
                case '\\':
                    sb.append("\\\\");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
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
}
