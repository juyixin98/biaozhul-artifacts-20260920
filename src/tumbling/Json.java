package tumbling;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析器 / 序列化器，仅依赖 JDK。
 * 解析结果使用：LinkedHashMap / ArrayList / String / Long / Double / Boolean / null。
 */
public final class Json {

    private Json() {}

    // ---------- 解析 ----------

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) throw new IllegalArgumentException("JSON 解析失败: 位置 " + p.pos + " 之后存在多余字符");
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) throw new IllegalArgumentException("期望 JSON 对象，实际为: " + typeName(v));
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
            if (eof()) throw new IllegalArgumentException("JSON 解析失败: 意外结束");
            char c = peek();
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': return readLiteral("true", Boolean.TRUE);
                case 'f': return readLiteral("false", Boolean.FALSE);
                case 'n': return readLiteral("null", null);
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return readNumber();
                    throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 出现非法字符 '" + c + "'");
            }
        }

        Object readLiteral(String lit, Object value) {
            if (!s.startsWith(lit, pos)) {
                throw new IllegalArgumentException("JSON 解析失败: 位置 " + pos + " 的字面量非法");
            }
            pos += lit.length();
            return value;
        }

        Map<String, Object> readObject() {
            Map<String, Object> m = new LinkedHashMap<>();
            pos++; // {
            skipWs();
            if (!eof() && peek() == '}') { pos++; return m; }
            while (true) {
                skipWs();
                if (eof() || peek() != '"') throw new IllegalArgumentException("JSON 解析失败: 对象键必须是字符串，位置 " + pos);
                String key = readString();
                skipWs();
                if (eof() || peek() != ':') throw new IllegalArgumentException("JSON 解析失败: 对象中缺少 ':'，位置 " + pos);
                pos++;
                skipWs();
                m.put(key, readValue());
                skipWs();
                if (eof()) throw new IllegalArgumentException("JSON 解析失败: 对象意外结束");
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return m; }
                throw new IllegalArgumentException("JSON 解析失败: 对象中期望 ',' 或 '}'，位置 " + pos);
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
                if (eof()) throw new IllegalArgumentException("JSON 解析失败: 数组意外结束");
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return list; }
                throw new IllegalArgumentException("JSON 解析失败: 数组中期望 ',' 或 ']'，位置 " + pos);
            }
        }

        String readString() {
            pos++; // 开引号
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw new IllegalArgumentException("JSON 解析失败: 字符串意外结束");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw new IllegalArgumentException("JSON 解析失败: 转义意外结束");
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
                            if (pos + 4 > s.length()) throw new IllegalArgumentException("JSON 解析失败: \\u 转义不完整");
                            String hex = s.substring(pos, pos + 4);
                            try { sb.append((char) Integer.parseInt(hex, 16)); }
                            catch (NumberFormatException ex) { throw new IllegalArgumentException("JSON 解析失败: 非法 \\u 转义 " + hex); }
                            pos += 4;
                            break;
                        default: throw new IllegalArgumentException("JSON 解析失败: 非法转义 \\" + e);
                    }
                } else if (c < 0x20) {
                    throw new IllegalArgumentException("JSON 解析失败: 字符串中存在未转义控制字符");
                } else {
                    sb.append(c);
                }
            }
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            int digitStart = pos;
            readDigits();
            // RFC 8259：整数部分不允许前导零（"01"/"-007" 非法；"0"/"-0"/"0.5" 合法）。
            if (pos - digitStart > 1 && s.charAt(digitStart) == '0') {
                throw new IllegalArgumentException("JSON 解析失败: 数字含前导零，位置 " + digitStart);
            }
            boolean isDouble = false;
            if (!eof() && peek() == '.') {
                isDouble = true;
                pos++;
                readDigits();
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                isDouble = true;
                pos++;
                if (!eof() && (peek() == '+' || peek() == '-')) pos++;
                readDigits();
            }
            String token = s.substring(start, pos);
            if (isDouble) {
                try {
                    return Double.parseDouble(token);
                } catch (NumberFormatException ex) {
                    throw new IllegalArgumentException("JSON 解析失败: 数字格式非法: " + token);
                }
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException ex) {
                // 超出 long 范围的整数不能静默降级为 double（会丢失精度并被截断使用）。
                throw new IllegalArgumentException("JSON 解析失败: 整数超出 long 范围: " + token);
            }
        }

        void readDigits() {
            int start = pos;
            while (!eof() && peek() >= '0' && peek() <= '9') pos++;
            if (pos == start) throw new IllegalArgumentException("JSON 解析失败: 数字格式非法，位置 " + pos);
        }
    }

    // ---------- 序列化 ----------

    public static String write(Object value) {
        return write(value, false);
    }

    public static String writePretty(Object value) {
        return write(value, true);
    }

    private static String write(Object value, boolean pretty) {
        StringBuilder sb = new StringBuilder();
        append(sb, value, pretty, 0);
        if (pretty) sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void append(StringBuilder sb, Object v, boolean pretty, int depth) {
        if (v == null) { sb.append("null"); return; }
        if (v instanceof String) { appendString(sb, (String) v); return; }
        if (v instanceof Boolean) { sb.append(v.toString()); return; }
        if (v instanceof Long || v instanceof Integer || v instanceof Short || v instanceof Byte) {
            sb.append(v.toString()); return;
        }
        if (v instanceof Number) { sb.append(v.toString()); return; }
        if (v instanceof Map) {
            Map<String, Object> m = (Map<String, Object>) v;
            if (m.isEmpty()) { sb.append("{}"); return; }
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                newline(sb, pretty, depth + 1);
                appendString(sb, e.getKey());
                sb.append(pretty ? ": " : ":");
                append(sb, e.getValue(), pretty, depth + 1);
            }
            newline(sb, pretty, depth);
            sb.append('}');
            return;
        }
        if (v instanceof Iterable) {
            List<Object> list = new ArrayList<>();
            for (Object o : (Iterable<Object>) v) list.add(o);
            if (list.isEmpty()) { sb.append("[]"); return; }
            sb.append('[');
            boolean first = true;
            for (Object o : list) {
                if (!first) sb.append(',');
                first = false;
                newline(sb, pretty, depth + 1);
                append(sb, o, pretty, depth + 1);
            }
            newline(sb, pretty, depth);
            sb.append(']');
            return;
        }
        if (v.getClass().isArray()) {
            int n = java.lang.reflect.Array.getLength(v);
            List<Object> list = new ArrayList<>(n);
            for (int i = 0; i < n; i++) list.add(java.lang.reflect.Array.get(v, i));
            append(sb, list, pretty, depth);
            return;
        }
        throw new IllegalArgumentException("无法序列化为 JSON 的类型: " + v.getClass().getName());
    }

    private static void newline(StringBuilder sb, boolean pretty, int depth) {
        if (!pretty) return;
        sb.append('\n');
        for (int i = 0; i < depth; i++) sb.append("  ");
    }

    private static void appendString(StringBuilder sb, String s) {
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
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
    }

    // ---------- 取值辅助 ----------

    public static String str(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof String)) {
            throw new IllegalArgumentException("字段 '" + key + "' 必须是字符串，实际为: " + typeName(v));
        }
        return (String) v;
    }

    public static long lng(Map<String, Object> m, String key) {
        Object v = m.get(key);
        // 只接受整数类型的数字，拒绝 15.9 / 1.0 等浮点 JSON 数，避免静默截断改变窗口归属。
        if (v instanceof Long || v instanceof Integer || v instanceof Short || v instanceof Byte) {
            return ((Number) v).longValue();
        }
        throw new IllegalArgumentException("字段 '" + key + "' 必须是整数，实际为: " + typeName(v));
    }

    public static boolean bool(Map<String, Object> m, String key, boolean dflt) {
        Object v = m.get(key);
        if (v == null) return dflt;
        if (!(v instanceof Boolean)) {
            throw new IllegalArgumentException("字段 '" + key + "' 必须是布尔值，实际为: " + typeName(v));
        }
        return (Boolean) v;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (!(v instanceof List)) {
            throw new IllegalArgumentException("字段 '" + key + "' 必须是数组，实际为: " + typeName(v));
        }
        return (List<Object>) v;
    }

    public static String typeName(Object v) {
        return v == null ? "null" : v.getClass().getSimpleName();
    }
}
