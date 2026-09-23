package colscan.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析 / 序列化，仅依赖 JDK。
 * 映射规则：object -> LinkedHashMap, array -> ArrayList,
 * string -> String, number -> Long 或 Double, true/false -> Boolean, null -> null(Json.Null)。
 * 为了区分 "null" 和“不存在”，parse 出来的 null 用单例 {@link Json.Null} 表示。
 */
public final class Json {

    public static final class JsonException extends RuntimeException {
        public JsonException(String msg) { super(msg); }
    }

    /** JSON null 的标记对象，便于和“键缺失”区分。 */
    public static final Object NULL = new Object() {
        @Override public String toString() { return "null"; }
    };

    private Json() {}

    // ---------------- 解析 ----------------

    public static Object parse(String s) {
        Parser p = new Parser(s);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != s.length()) {
            throw new JsonException("trailing characters at " + p.pos);
        }
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String s) {
        Object v = parse(s);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) { this.s = s; }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    return;
                }
            }
        }

        Object readValue() {
            if (pos >= s.length()) throw new JsonException("unexpected end of input");
            char c = s.charAt(pos);
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return readString();
                case 't': return readLiteral("true", Boolean.TRUE);
                case 'f': return readLiteral("false", Boolean.FALSE);
                case 'n': return readLiteral("null", NULL);
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return readNumber();
                    throw new JsonException("unexpected character '" + c + "' at " + pos);
            }
        }

        Object readLiteral(String literal, Object value) {
            if (!s.startsWith(literal, pos)) {
                throw new JsonException("invalid literal at " + pos);
            }
            pos += literal.length();
            return value;
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            pos++; // {
            skipWs();
            if (pos < s.length() && s.charAt(pos) == '}') { pos++; return map; }
            while (true) {
                skipWs();
                if (pos >= s.length() || s.charAt(pos) != '"') {
                    throw new JsonException("expected string key at " + pos);
                }
                String key = readString();
                skipWs();
                if (pos >= s.length() || s.charAt(pos) != ':') {
                    throw new JsonException("expected ':' at " + pos);
                }
                pos++;
                skipWs();
                map.put(key, readValue());
                skipWs();
                if (pos >= s.length()) throw new JsonException("unterminated object");
                char c = s.charAt(pos);
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return map; }
                throw new JsonException("expected ',' or '}' at " + pos);
            }
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            pos++; // [
            skipWs();
            if (pos < s.length() && s.charAt(pos) == ']') { pos++; return list; }
            while (true) {
                skipWs();
                list.add(readValue());
                skipWs();
                if (pos >= s.length()) throw new JsonException("unterminated array");
                char c = s.charAt(pos);
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return list; }
                throw new JsonException("expected ',' or ']' at " + pos);
            }
        }

        String readString() {
            pos++; // opening quote
            StringBuilder sb = new StringBuilder();
            while (pos < s.length()) {
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (pos >= s.length()) throw new JsonException("unterminated escape");
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
                            if (pos + 4 > s.length()) throw new JsonException("bad unicode escape");
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                            break;
                        default: throw new JsonException("bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
            throw new JsonException("unterminated string");
        }

        Object readNumber() {
            int start = pos;
            if (s.charAt(pos) == '-') pos++;
            readDigits();
            boolean isDouble = false;
            if (pos < s.length() && s.charAt(pos) == '.') {
                isDouble = true;
                pos++;
                readDigits();
            }
            if (pos < s.length() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                isDouble = true;
                pos++;
                if (pos < s.length() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) pos++;
                readDigits();
            }
            String token = s.substring(start, pos);
            if (isDouble) {
                double d = Double.parseDouble(token);
                if (!Double.isFinite(d)) throw new JsonException("non-finite number: " + token);
                return d;
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException ex) {
                // 超出 long 范围的整数：按 double 处理
                double d = Double.parseDouble(token);
                if (!Double.isFinite(d)) throw new JsonException("non-finite number: " + token);
                return d;
            }
        }

        void readDigits() {
            int start = pos;
            while (pos < s.length() && s.charAt(pos) >= '0' && s.charAt(pos) <= '9') pos++;
            if (start == pos) throw new JsonException("expected digits at " + pos);
        }
    }

    // ---------------- 序列化 ----------------

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    public static String pretty(Object v) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, v, 0);
        sb.append('\n');
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null || v == NULL) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v) ? "true" : "false");
        } else if (v instanceof Long || v instanceof Integer || v instanceof Short
                || v instanceof Byte) {
            sb.append(((Number) v).longValue());
        } else if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (!Double.isFinite(d)) throw new JsonException("cannot serialize non-finite double");
            sb.append(d);
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<String, Object> e : ((Map<String, Object>) v).entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, e.getKey());
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable) {
            sb.append('[');
            boolean first = true;
            for (Object item : (Iterable<Object>) v) {
                if (!first) sb.append(',');
                first = false;
                writeTo(sb, item);
            }
            sb.append(']');
        } else if (v.getClass().isArray()) {
            sb.append('[');
            int n = java.lang.reflect.Array.getLength(v);
            for (int i = 0; i < n; i++) {
                if (i > 0) sb.append(',');
                writeTo(sb, java.lang.reflect.Array.get(v, i));
            }
            sb.append(']');
        } else {
            throw new JsonException("cannot serialize " + v.getClass());
        }
    }

    @SuppressWarnings("unchecked")
    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map) {
            Map<String, Object> map = (Map<String, Object>) v;
            if (map.isEmpty()) { sb.append("{}"); return; }
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<String, Object> e : map.entrySet()) {
                if (!first) sb.append(",\n");
                first = false;
                indent(sb, indent + 1);
                writeString(sb, e.getKey());
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append('}');
        } else if (v instanceof Iterable) {
            List<Object> list = new ArrayList<>();
            for (Object o : (Iterable<Object>) v) list.add(o);
            if (list.isEmpty()) { sb.append("[]"); return; }
            sb.append("[\n");
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append(",\n");
                indent(sb, indent + 1);
                writePretty(sb, list.get(i), indent + 1);
            }
            sb.append('\n');
            indent(sb, indent);
            sb.append(']');
        } else {
            writeTo(sb, v);
        }
    }

    private static void indent(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
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
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        sb.append('"');
    }

    // ---------------- 取值辅助 ----------------

    public static Long asLong(Object v) {
        if (v instanceof Long) return (Long) v;
        if (v instanceof Number) return ((Number) v).longValue();
        return null;
    }

    public static String asString(Object v) {
        return v instanceof String ? (String) v : null;
    }
}
