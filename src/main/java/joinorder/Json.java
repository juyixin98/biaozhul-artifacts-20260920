package joinorder;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析 / 序列化（零第三方依赖）。
 *
 * 解析结果的 Java 类型映射：
 *   object -> LinkedHashMap&lt;String,Object&gt;（保持键顺序）
 *   array  -> ArrayList&lt;Object&gt;
 *   string -> String
 *   number -> Long（无小数/指数）或 Double
 *   true/false -> Boolean，null -> null
 */
public final class Json {

    private Json() {}

    // ---------------------------------------------------------------- parsing

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.ws();
        Object v = p.value();
        p.ws();
        if (p.i < p.s.length()) {
            throw new EngineException("JSON 解析失败：位置 " + p.i + " 之后存在多余字符");
        }
        return v;
    }

    private static final class Parser {
        final String s;
        int i;

        Parser(String s) { this.s = s; }

        void ws() {
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') i++;
                else break;
            }
        }

        void expect(char c) {
            if (i >= s.length() || s.charAt(i) != c) {
                throw new EngineException("JSON 解析失败：位置 " + i + " 期望 '" + c + "'");
            }
            i++;
        }

        Object value() {
            ws();
            if (i >= s.length()) throw new EngineException("JSON 解析失败：意外的结尾");
            char c = s.charAt(i);
            if (c == '{') return object();
            if (c == '[') return array();
            if (c == '"') return string();
            if (c == 't' || c == 'f') return bool();
            if (c == 'n') return nul();
            return number();
        }

        Map<String, Object> object() {
            expect('{');
            Map<String, Object> m = new LinkedHashMap<>();
            ws();
            if (i < s.length() && s.charAt(i) == '}') { i++; return m; }
            while (true) {
                ws();
                String key = string();
                ws();
                expect(':');
                Object val = value();
                m.put(key, val);
                ws();
                char c = s.charAt(i++);
                if (c == '}') return m;
                if (c != ',') throw new EngineException("JSON 解析失败：位置 " + i + " 期望 ',' 或 '}'");
            }
        }

        List<Object> array() {
            expect('[');
            List<Object> list = new ArrayList<>();
            ws();
            if (i < s.length() && s.charAt(i) == ']') { i++; return list; }
            while (true) {
                list.add(value());
                ws();
                char c = s.charAt(i++);
                if (c == ']') return list;
                if (c != ',') throw new EngineException("JSON 解析失败：位置 " + i + " 期望 ',' 或 ']'");
            }
        }

        String string() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (i < s.length()) {
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (i >= s.length()) break;
                    char e = s.charAt(i++);
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
                            if (i + 4 > s.length()) throw new EngineException("JSON 解析失败：非法 \\u 转义");
                            sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            i += 4;
                            break;
                        default: throw new EngineException("JSON 解析失败：非法转义 \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
            throw new EngineException("JSON 解析失败：字符串未闭合");
        }

        Boolean bool() {
            if (s.startsWith("true", i)) { i += 4; return Boolean.TRUE; }
            if (s.startsWith("false", i)) { i += 5; return Boolean.FALSE; }
            throw new EngineException("JSON 解析失败：位置 " + i + " 非法字面量");
        }

        Object nul() {
            if (s.startsWith("null", i)) { i += 4; return null; }
            throw new EngineException("JSON 解析失败：位置 " + i + " 非法字面量");
        }

        Object number() {
            int start = i;
            if (i < s.length() && s.charAt(i) == '-') i++;
            boolean fractional = false;
            while (i < s.length()) {
                char c = s.charAt(i);
                if (c >= '0' && c <= '9') { i++; }
                else if (c == '.' || c == 'e' || c == 'E') {
                    fractional = true;
                    i++;
                    if (i < s.length() && (s.charAt(i) == '+' || s.charAt(i) == '-')) i++;
                } else break;
            }
            String tok = s.substring(start, i);
            if (tok.isEmpty() || tok.equals("-")) {
                throw new EngineException("JSON 解析失败：位置 " + start + " 非法数字");
            }
            try {
                if (fractional) return Double.valueOf(Double.parseDouble(tok));
                return Long.valueOf(Long.parseLong(tok));
            } catch (NumberFormatException ex) {
                throw new EngineException("JSON 解析失败：非法数字 " + tok);
            }
        }
    }

    // ------------------------------------------------------------- printing

    public static String write(Object v) { return write(v, true); }

    public static String write(Object v, boolean pretty) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, v, pretty ? 0 : -1);
        sb.append('\n');
        return sb.toString();
    }

    private static void indent(StringBuilder sb, int depth) {
        for (int k = 0; k < depth; k++) sb.append("  ");
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v, int depth) {
        if (v == null) { sb.append("null"); return; }
        if (v instanceof String) { writeString(sb, (String) v); return; }
        if (v instanceof Boolean) { sb.append(v.toString()); return; }
        if (v instanceof Long || v instanceof Integer) { sb.append(v.toString()); return; }
        if (v instanceof Double || v instanceof Float) {
            double d = ((Number) v).doubleValue();
            if (Double.isNaN(d) || Double.isInfinite(d)) {
                sb.append("null"); // JSON 无法表达
            } else if (d == Math.floor(d) && !Double.isInfinite(d)
                    && d >= -9.007199254740992E15 && d <= 9.007199254740992E15) {
                sb.append(Long.toString((long) d));
            } else {
                sb.append(Double.toString(d));
            }
            return;
        }
        if (v instanceof Map) {
            Map<String, Object> m = (Map<String, Object>) v;
            if (m.isEmpty()) { sb.append("{}"); return; }
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                if (depth >= 0) { sb.append('\n'); indent(sb, depth + 1); }
                writeString(sb, e.getKey());
                sb.append(depth >= 0 ? ": " : ":");
                writeValue(sb, e.getValue(), depth >= 0 ? depth + 1 : -1);
            }
            if (depth >= 0) { sb.append('\n'); indent(sb, depth); }
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
                if (depth >= 0) { sb.append('\n'); indent(sb, depth + 1); }
                writeValue(sb, o, depth >= 0 ? depth + 1 : -1);
            }
            if (depth >= 0) { sb.append('\n'); indent(sb, depth); }
            sb.append(']');
            return;
        }
        writeString(sb, v.toString());
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
    }

    // ------------------------------------------------------------- accessors

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object o, String ctx) {
        if (!(o instanceof Map)) throw new EngineException(ctx + " 应为 JSON 对象");
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArray(Object o, String ctx) {
        if (!(o instanceof List)) throw new EngineException(ctx + " 应为 JSON 数组");
        return (List<Object>) o;
    }

    public static Map<String, Object> obj(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (!(o instanceof Map)) throw new EngineException("字段 '" + key + "' 应为 JSON 对象");
        return asObject(o, key);
    }

    public static List<Object> arr(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (!(o instanceof List)) throw new EngineException("字段 '" + key + "' 应为 JSON 数组");
        return asArray(o, key);
    }

    public static String str(Map<String, Object> m, String key) {
        Object o = m.get(key);
        if (!(o instanceof String)) throw new EngineException("字段 '" + key + "' 应为字符串");
        return (String) o;
    }

    public static String strOr(Map<String, Object> m, String key, String dflt) {
        Object o = m.get(key);
        return o == null ? dflt : o.toString();
    }

    public static long lng(Map<String, Object> m, String key, long dflt) {
        Object o = m.get(key);
        if (o == null) return dflt;
        if (!(o instanceof Number)) throw new EngineException("字段 '" + key + "' 应为整数");
        double d = ((Number) o).doubleValue();
        if (d != Math.floor(d)) throw new EngineException("字段 '" + key + "' 应为整数");
        return (long) d;
    }

    public static boolean bool(Map<String, Object> m, String key, boolean dflt) {
        Object o = m.get(key);
        if (o == null) return dflt;
        if (!(o instanceof Boolean)) throw new EngineException("字段 '" + key + "' 应为布尔值");
        return (Boolean) o;
    }
}
