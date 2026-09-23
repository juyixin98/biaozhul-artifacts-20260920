package phj.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析器 / 序列化器（零外部依赖）。
 *
 * 支持的 Java 映射：
 *   object -> LinkedHashMap&lt;String,Object&gt;（保留插入顺序）
 *   array  -> ArrayList&lt;Object&gt;
 *   string -> String
 *   number -> Long（整数）或 Double（含小数 / 指数 / 溢出 long 范围）
 *   true/false -> Boolean
 *   null -> null
 *
 * 仅用于本项目的请求/落盘格式，不追求完整 RFC 8259 合规，但覆盖常见输入。
 */
public final class Json {

    private Json() {}

    // ---------------------------------------------------------------- 解析

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("JSON 解析失败：位置 " + p.pos + " 之后仍有多余字符");
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("JSON 解析失败：期望顶层为 object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }

        char peek() { return s.charAt(pos); }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        Object readValue() {
            if (eof()) throw new JsonException("JSON 解析失败：意外结束");
            char c = peek();
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': case 'f': return readBool();
                case 'n': return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return readNumber();
                    throw new JsonException("JSON 解析失败：位置 " + pos + " 处出现非法字符 '" + c + "'");
            }
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            pos++; // {
            skipWs();
            if (!eof() && peek() == '}') { pos++; return map; }
            while (true) {
                skipWs();
                if (eof() || peek() != '"') {
                    throw new JsonException("JSON 解析失败：位置 " + pos + " 处期望字符串键");
                }
                String key = readString();
                skipWs();
                if (eof() || peek() != ':') {
                    throw new JsonException("JSON 解析失败：位置 " + pos + " 处期望 ':'");
                }
                pos++;
                skipWs();
                Object val = readValue();
                map.put(key, val);
                skipWs();
                if (eof()) throw new JsonException("JSON 解析失败：object 未闭合");
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return map; }
                throw new JsonException("JSON 解析失败：位置 " + pos + " 处期望 ',' 或 '}'，实际 '" + c + "'");
            }
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            pos++; // [
            skipWs();
            if (!eof() && peek() == ']') { pos++; return list; }
            while (true) {
                skipWs();
                list.add(readValue());
                skipWs();
                if (eof()) throw new JsonException("JSON 解析失败：array 未闭合");
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return list; }
                throw new JsonException("JSON 解析失败：位置 " + pos + " 处期望 ',' 或 ']'，实际 '" + c + "'");
            }
        }

        String readString() {
            StringBuilder sb = new StringBuilder();
            pos++; // 开引号
            while (true) {
                if (eof()) throw new JsonException("JSON 解析失败：字符串未闭合");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw new JsonException("JSON 解析失败：转义未结束");
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
                            if (pos + 4 > s.length()) throw new JsonException("JSON 解析失败：\\u 转义不完整");
                            String hex = s.substring(pos, pos + 4);
                            pos += 4;
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException nfe) {
                                throw new JsonException("JSON 解析失败：非法 \\u 转义 " + hex);
                            }
                            break;
                        default:
                            throw new JsonException("JSON 解析失败：非法转义 \\" + e);
                    }
                } else if (c < 0x20) {
                    throw new JsonException("JSON 解析失败：字符串中存在未转义控制字符 0x"
                            + Integer.toHexString(c));
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBool() {
            if (s.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw new JsonException("JSON 解析失败：位置 " + pos + " 处非法字面量");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) { pos += 4; return null; }
            throw new JsonException("JSON 解析失败：位置 " + pos + " 处非法字面量");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            while (!eof() && Character.isDigit(peek())) pos++;
            boolean isDouble = false;
            if (!eof() && peek() == '.') {
                isDouble = true;
                pos++;
                while (!eof() && Character.isDigit(peek())) pos++;
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                isDouble = true;
                pos++;
                if (!eof() && (peek() == '+' || peek() == '-')) pos++;
                while (!eof() && Character.isDigit(peek())) pos++;
            }
            String num = s.substring(start, pos);
            try {
                if (!isDouble) return Long.parseLong(num);
                return Double.parseDouble(num);
            } catch (NumberFormatException nfe) {
                // 超出 long 的整数也退化为 double
                try {
                    return Double.parseDouble(num);
                } catch (NumberFormatException nfe2) {
                    throw new JsonException("JSON 解析失败：非法数字 " + num);
                }
            }
        }
    }

    // ---------------------------------------------------------------- 序列化

    public static String write(Object o) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, o);
        return sb.toString();
    }

    public static String writePretty(Object o) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, o, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writePretty(StringBuilder sb, Object o, int indent) {
        if (o == null) {
            sb.append("null");
        } else if (o instanceof Map<?, ?> m) {
            if (m.isEmpty()) { sb.append("{}"); return; }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(",\n");
                first = false;
                pad(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append('}');
        } else if (o instanceof List<?> l) {
            if (l.isEmpty()) { sb.append("[]"); return; }
            sb.append("[\n");
            boolean first = true;
            for (Object item : l) {
                if (!first) sb.append(",\n");
                first = false;
                pad(sb, indent + 1);
                writePretty(sb, item, indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append(']');
        } else {
            writeTo(sb, o);
        }
    }

    private static void pad(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
    }

    @SuppressWarnings("unchecked")
    private static void writeTo(StringBuilder sb, Object o) {
        if (o == null) {
            sb.append("null");
        } else if (o instanceof String str) {
            writeString(sb, str);
        } else if (o instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (o instanceof Long || o instanceof Integer || o instanceof Short || o instanceof Byte) {
            sb.append(((Number) o).longValue());
        } else if (o instanceof Double || o instanceof Float) {
            double d = ((Number) o).doubleValue();
            if (Double.isFinite(d)) {
                // 规范 JSON 不允许 Infinity/NaN；能整表示时输出 .0 保留 double 类型特征并非必须，
                // 这里用最短标准形式即可（数字按数值比较，1 与 1.0 等价）。
                sb.append(d);
            } else {
                throw new JsonException("JSON 序列化失败：非有限数值 " + d);
            }
        } else if (o instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (o instanceof List<?> l) {
            sb.append('[');
            boolean first = true;
            for (Object item : l) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, item);
            }
            sb.append(']');
        } else if (o instanceof Object[] arr) {
            writeTo(sb, java.util.Arrays.asList(arr));
        } else {
            throw new JsonException("JSON 序列化失败：不支持的类型 " + o.getClass().getName());
        }
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

    // ---------------------------------------------------- 便捷取值（带类型检查）

    public static Map<String, Object> asObj(Object o, String ctx) {
        if (o instanceof Map<?, ?> m) {
            @SuppressWarnings("unchecked")
            Map<String, Object> casted = (Map<String, Object>) m;
            return casted;
        }
        throw new JsonException(ctx + " 应为 JSON object");
    }

    public static List<Object> asArr(Object o, String ctx) {
        if (o instanceof List<?> l) {
            @SuppressWarnings("unchecked")
            List<Object> casted = (List<Object>) l;
            return casted;
        }
        throw new JsonException(ctx + " 应为 JSON array");
    }

    public static String asStr(Object o, String ctx) {
        if (o instanceof String s) return s;
        throw new JsonException(ctx + " 应为字符串");
    }

    public static long asLong(Object o, String ctx) {
        if (o instanceof Number n) return n.longValue();
        throw new JsonException(ctx + " 应为整数");
    }

    public static String optStr(Map<String, Object> m, String key, String dflt) {
        Object v = m.get(key);
        return v == null ? dflt : asStr(v, "字段 '" + key + "'");
    }

    public static long optLong(Map<String, Object> m, String key, long dflt) {
        Object v = m.get(key);
        return v == null ? dflt : asLong(v, "字段 '" + key + "'");
    }
}
