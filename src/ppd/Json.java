package ppd;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析器 / 序列化器（无第三方依赖）。
 *
 * 支持的 Java 映射：
 *   object -> LinkedHashMap&lt;String,Object&gt;
 *   array  -> List&lt;Object&gt;
 *   string -> String
 *   number -> Long（整数）或 Double
 *   true/false -> Boolean
 *   null   -> null
 */
public final class Json {

    private Json() {}

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) throw p.err("尾随字符");
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String text) {
        Object o = parse(text);
        if (!(o instanceof Map)) throw new RuntimeException("JSON 顶层不是对象");
        return (Map<String, Object>) o;
    }

    public static String render(Object value) {
        StringBuilder sb = new StringBuilder();
        write(sb, value);
        return sb.toString();
    }

    public static String renderPretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    // ------------------------------------------------------------------
    // 类型安全的取值辅助
    // ------------------------------------------------------------------

    @SuppressWarnings("unchecked")
    public static Map<String, Object> obj(Object o) {
        return (Map<String, Object>) o;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> arr(Object o) {
        return (List<Object>) o;
    }

    public static Map<String, Object> getObj(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) return null;
        return obj(v);
    }

    public static List<Map<String, Object>> getObjList(Map<String, Object> m, String key) {
        Object v = m.get(key);
        if (v == null) return List.of();
        List<Map<String, Object>> out = new ArrayList<>();
        for (Object e : arr(v)) out.add(obj(e));
        return out;
    }

    public static String getStr(Map<String, Object> m, String key) {
        Object v = m.get(key);
        return v == null ? null : v.toString();
    }

    public static boolean getBool(Map<String, Object> m, String key, boolean dflt) {
        Object v = m.get(key);
        return v instanceof Boolean b ? b : dflt;
    }

    // ------------------------------------------------------------------
    // 序列化
    // ------------------------------------------------------------------

    private static void write(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String s) {
            writeString(sb, s);
        } else if (v instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (v instanceof Long l) {
            sb.append(l.toString());
        } else if (v instanceof Integer i) {
            sb.append(i.toString());
        } else if (v instanceof Double d) {
            if (d.isNaN() || d.isInfinite()) sb.append("null");
            else sb.append(d.toString());
        } else if (v instanceof Number n) {
            sb.append(n.toString());
        } else if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, e.getKey().toString());
                sb.append(':');
                write(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object e : it) {
                if (!first) sb.append(',');
                first = false;
                write(sb, e);
            }
            sb.append(']');
        } else {
            writeString(sb, v.toString());
        }
    }

    private static void writePretty(StringBuilder sb, Object v, int indent) {
        if (v instanceof Map<?, ?> m) {
            if (m.isEmpty()) { sb.append("{}"); return; }
            sb.append('{').append('\n');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(",\n");
                first = false;
                pad(sb, indent + 1);
                writeString(sb, e.getKey().toString());
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append('}');
        } else if (v instanceof Iterable<?> it && !(v instanceof String)) {
            List<Object> list = new ArrayList<>();
            for (Object e : it) list.add(e);
            if (list.isEmpty()) { sb.append("[]"); return; }
            sb.append('[').append('\n');
            for (int i = 0; i < list.size(); i++) {
                if (i > 0) sb.append(",\n");
                pad(sb, indent + 1);
                writePretty(sb, list.get(i), indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append(']');
        } else {
            write(sb, v);
        }
    }

    private static void pad(StringBuilder sb, int level) {
        sb.append("  ".repeat(level));
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
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    // ------------------------------------------------------------------
    // 解析
    // ------------------------------------------------------------------

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }

        RuntimeException err(String msg) {
            return new RuntimeException("JSON 解析错误(pos=" + pos + "): " + msg);
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        char peek() { return s.charAt(pos); }

        Object readValue() {
            skipWs();
            if (eof()) throw err("意外结束");
            char c = peek();
            if (c == '{') return readObject();
            if (c == '[') return readArray();
            if (c == '"') return readString();
            if (c == 't' || c == 'f') return readBool();
            if (c == 'n') return readNull();
            return readNumber();
        }

        Map<String, Object> readObject() {
            Map<String, Object> m = new LinkedHashMap<>();
            expect('{');
            skipWs();
            if (peek() == '}') { pos++; return m; }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object val = readValue();
                m.put(key, val);
                skipWs();
                char c = next();
                if (c == '}') break;
                if (c != ',') throw err("期望 ',' 或 '}'");
            }
            return m;
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            expect('[');
            skipWs();
            if (peek() == ']') { pos++; return list; }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ']') break;
                if (c != ',') throw err("期望 ',' 或 ']'");
            }
            return list;
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw err("字符串未闭合");
                char c = next();
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    char e = next();
                    switch (e) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'n' -> sb.append('\n');
                        case 'r' -> sb.append('\r');
                        case 't' -> sb.append('\t');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'u' -> {
                            if (pos + 4 > s.length()) throw err("\\u 转义不完整");
                            int code = Integer.parseInt(s.substring(pos, pos + 4), 16);
                            sb.append((char) code);
                            pos += 4;
                        }
                        default -> throw err("非法转义 \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBool() {
            if (s.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw err("非法字面量");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) { pos += 4; return null; }
            throw err("非法字面量");
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            while (!eof() && (Character.isDigit(peek()) || peek() == '.'
                    || peek() == 'e' || peek() == 'E' || peek() == '+' || peek() == '-')) {
                pos++;
            }
            String tok = s.substring(start, pos);
            if (tok.isEmpty()) throw err("非法数字");
            try {
                if (!tok.contains(".") && !tok.contains("e") && !tok.contains("E")) {
                    return Long.parseLong(tok);
                }
                return Double.parseDouble(tok);
            } catch (NumberFormatException ex) {
                throw err("非法数字 " + tok);
            }
        }

        void expect(char c) {
            if (eof() || s.charAt(pos) != c) throw err("期望 '" + c + "'");
            pos++;
        }

        char next() {
            if (eof()) throw err("意外结束");
            return s.charAt(pos++);
        }
    }
}
