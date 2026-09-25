package com.example.cptx.core;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极小的依赖无关 JSON 解析/写入器。
 *
 * 支持：object、array、字符串转义（含反斜杠 u 四位十六进制形式与代理对）、long/double、true/false/null。
 * 整数解析为 Long，带小数点/指数的解析为 Double。
 * 写出时 Map 按 key 排序，保证文件内容确定，便于逐字节比较。
 */
public final class Json {

    private Json() {}

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object v) { return (Map<String, Object>) v; }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object v) { return (List<Object>) v; }

    public static String str(Object v) { return v == null ? null : (String) v; }

    public static long lng(Object v) { return ((Number) v).longValue(); }

    public static double dbl(Object v) { return ((Number) v).doubleValue(); }

    public static boolean bool(Object v) { return Boolean.TRUE.equals(v); }

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.ws();
        Object v = p.value();
        p.ws();
        if (p.pos != p.s.length()) {
            throw new IllegalArgumentException("JSON 解析失败：位置 " + p.pos + " 之后仍有多余字符");
        }
        return v;
    }

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb, 0, false);
        return sb.toString();
    }

    public static String pretty(Object v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb, 0, true);
        sb.append('\n');
        return sb.toString();
    }

    private static void write(Object v, StringBuilder sb, int depth, boolean pretty) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(s, sb);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Long l) {
            sb.append(l.longValue());
        } else if (v instanceof Integer i) {
            sb.append(i.intValue());
        } else if (v instanceof Number n) {
            double d = n.doubleValue();
            if (!Double.isFinite(d)) {
                throw new IllegalArgumentException("不允许 NaN/Infinity 出现在 JSON 中");
            }
            sb.append(Double.toString(d));
        } else if (v instanceof Map<?, ?> map) {
            List<String> keys = new ArrayList<>();
            for (Object k : map.keySet()) {
                keys.add((String) k);
            }
            keys.sort(String::compareTo);
            if (keys.isEmpty()) {
                sb.append("{}");
                return;
            }
            sb.append('{');
            boolean first = true;
            for (String k : keys) {
                if (!first) sb.append(',');
                first = false;
                if (pretty) sb.append('\n').append("  ".repeat(depth + 1));
                writeString(k, sb);
                sb.append(':');
                if (pretty) sb.append(' ');
                write(map.get(k), sb, depth + 1, pretty);
            }
            if (pretty) sb.append('\n').append("  ".repeat(depth));
            sb.append('}');
        } else if (v instanceof List<?> list) {
            if (list.isEmpty()) {
                sb.append("[]");
                return;
            }
            sb.append('[');
            boolean first = true;
            for (Object e : list) {
                if (!first) sb.append(',');
                first = false;
                if (pretty) sb.append('\n').append("  ".repeat(depth + 1));
                write(e, sb, depth + 1, pretty);
            }
            if (pretty) sb.append('\n').append("  ".repeat(depth));
            sb.append(']');
        } else {
            throw new IllegalArgumentException("不支持的 JSON 类型: " + v.getClass());
        }
    }

    private static void writeString(String s, StringBuilder sb) {
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

    public static Map<String, Object> newObject() {
        return new LinkedHashMap<>();
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        void ws() {
            while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) pos++;
        }

        char peek() {
            if (pos >= s.length()) {
                throw new IllegalArgumentException("JSON 解析失败：意外的结尾，位置 " + pos);
            }
            return s.charAt(pos);
        }

        Object value() {
            ws();
            char c = peek();
            return switch (c) {
                case '"' -> string();
                case '{' -> object();
                case '[' -> array();
                case 't', 'f' -> bool();
                case 'n' -> nul();
                default -> {
                    if (c == '-' || (c >= '0' && c <= '9')) yield number();
                    throw new IllegalArgumentException("JSON 解析失败：意外字符 '" + c + "'，位置 " + pos);
                }
            };
        }

        Map<String, Object> object() {
            Map<String, Object> m = new LinkedHashMap<>();
            expect('{');
            ws();
            if (peek() == '}') { pos++; return m; }
            while (true) {
                ws();
                String k = string();
                ws();
                expect(':');
                Object v = value();
                m.put(k, v);
                ws();
                char c = next();
                if (c == '}') return m;
                if (c != ',') throw new IllegalArgumentException("JSON 解析失败：期望 ',' 或 '}}'，位置 " + pos);
            }
        }

        List<Object> array() {
            List<Object> list = new ArrayList<>();
            expect('[');
            ws();
            if (peek() == ']') { pos++; return list; }
            while (true) {
                list.add(value());
                ws();
                char c = next();
                if (c == ']') return list;
                if (c != ',') throw new IllegalArgumentException("JSON 解析失败：期望 ',' 或 ']'，位置 " + pos);
            }
        }

        String string() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = next();
                if (c == '"') return sb.toString();
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
                            char hi = unicodeHex();
                            if (Character.isHighSurrogate(hi) && pos + 1 < s.length()
                                    && s.charAt(pos) == '\\' && s.charAt(pos + 1) == 'u') {
                                pos += 2;
                                char lo = unicodeHex();
                                sb.append(hi).append(lo);
                            } else {
                                sb.append(hi);
                            }
                        }
                        default -> throw new IllegalArgumentException("JSON 解析失败：非法转义 \\" + e);
                    }
                } else if (c < 0x20) {
                    throw new IllegalArgumentException("JSON 解析失败：字符串中存在未转义控制字符");
                } else {
                    sb.append(c);
                }
            }
        }

        char unicodeHex() {
            if (pos + 4 > s.length()) {
                throw new IllegalArgumentException("JSON 解析失败：\\uXXXX 不完整");
            }
            String hex = s.substring(pos, pos + 4);
            pos += 4;
            try {
                return (char) Integer.parseInt(hex, 16);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("JSON 解析失败：非法 Unicode 转义 \\u" + hex);
            }
        }

        Object bool() {
            if (s.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw new IllegalArgumentException("JSON 解析失败：应为 true/false，位置 " + pos);
        }

        Object nul() {
            if (s.startsWith("null", pos)) { pos += 4; return null; }
            throw new IllegalArgumentException("JSON 解析失败：应为 null，位置 " + pos);
        }

        Object number() {
            int start = pos;
            if (peek() == '-') pos++;
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if ((c >= '0' && c <= '9') || c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    pos++;
                } else {
                    break;
                }
            }
            String token = s.substring(start, pos);
            boolean floating = token.contains(".") || token.contains("e") || token.contains("E");
            try {
                if (!floating) return Long.parseLong(token);
            } catch (NumberFormatException ignore) {
                // 落在 double 分支
            }
            return Double.parseDouble(token);
        }

        char next() {
            if (pos >= s.length()) {
                throw new IllegalArgumentException("JSON 解析失败：意外的结尾，位置 " + pos);
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            char actual = next();
            if (actual != c) {
                throw new IllegalArgumentException(
                        "JSON 解析失败：期望 '" + c + "'，实际 '" + actual + "'，位置 " + (pos - 1));
            }
        }
    }
}
