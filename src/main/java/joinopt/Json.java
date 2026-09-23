package joinopt;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析与序列化（零外部依赖）。
 *
 * JSON 值在 Java 侧的表示：
 *   object -> {@link LinkedHashMap}（保留键顺序）
 *   array  -> {@link ArrayList}
 *   string -> {@link String}
 *   number -> {@link Long}（整数）或 {@link Double}（含小数/指数）
 *   true/false -> {@link Boolean}
 *   null   -> Java null
 */
public final class Json {

    private Json() {}

    public static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new IllegalArgumentException("JSON 解析失败: 第 " + p.line() + " 行附近有多余字符");
        }
        return v;
    }

    public static String pretty(Object v) {
        StringBuilder sb = new StringBuilder();
        write(sb, v, 0);
        sb.append('\n');
        return sb.toString();
    }

    // ---------- 序列化 ----------

    private static void write(StringBuilder sb, Object v, int indent) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            sb.append(quote((String) v));
        } else if (v instanceof Boolean) {
            sb.append(((Boolean) v) ? "true" : "false");
        } else if (v instanceof Map) {
            writeMap(sb, (Map<?, ?>) v, indent);
        } else if (v instanceof List) {
            writeList(sb, (List<?>) v, indent);
        } else {
            // Long / Double / Integer ... 直接输出；Double 保证 NaN/Infinity 不会出现
            sb.append(v.toString());
        }
    }

    private static void writeMap(StringBuilder sb, Map<?, ?> map, int indent) {
        if (map.isEmpty()) {
            sb.append("{}");
            return;
        }
        sb.append("{\n");
        boolean first = true;
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!first) sb.append(",\n");
            first = false;
            pad(sb, indent + 1);
            sb.append(quote(String.valueOf(e.getKey()))).append(": ");
            write(sb, e.getValue(), indent + 1);
        }
        sb.append('\n');
        pad(sb, indent);
        sb.append('}');
    }

    private static void writeList(StringBuilder sb, List<?> list, int indent) {
        if (list.isEmpty()) {
            sb.append("[]");
            return;
        }
        sb.append("[\n");
        boolean first = true;
        for (Object item : list) {
            if (!first) sb.append(",\n");
            first = false;
            pad(sb, indent + 1);
            write(sb, item, indent + 1);
        }
        sb.append('\n');
        pad(sb, indent);
        sb.append(']');
    }

    private static void pad(StringBuilder sb, int indent) {
        for (int i = 0; i < indent; i++) sb.append("  ");
    }

    private static String quote(String s) {
        StringBuilder sb = new StringBuilder();
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"':  sb.append("\\\""); break;
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
        return sb.toString();
    }

    // ---------- 类型安全取值辅助 ----------

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObj(Object v) {
        return (Map<String, Object>) v;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArr(Object v) {
        return (List<Object>) v;
    }

    /** 取字符串字段，缺失或为 null 返回 null。 */
    public static String str(Map<?, ?> m, String key) {
        Object v = m.get(key);
        return v == null ? null : String.valueOf(v);
    }

    /** 取长整型字段；接受数字或数字字符串。 */
    public static long lng(Map<?, ?> m, String key) {
        return asLong(m.get(key));
    }

    public static long asLong(Object v) {
        if (v instanceof Number) return ((Number) v).longValue();
        if (v instanceof String) return Long.parseLong((String) v);
        throw new IllegalArgumentException("期望整数, 实际: " + v);
    }

    public static int asInt(Object v) {
        return (int) asLong(v);
    }

    public static double asDouble(Object v) {
        if (v instanceof Number) return ((Number) v).doubleValue();
        if (v instanceof String) return Double.parseDouble((String) v);
        throw new IllegalArgumentException("期望数字, 实际: " + v);
    }

    /** 取对象数组字段；标量缺失时按缺失处理（返回 null）。 */
    public static List<Object> arr(Map<?, ?> m, String key) {
        Object v = m.get(key);
        return v == null ? null : asArr(v);
    }

    // ---------- 解析器 ----------

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }
        char peek() { return s.charAt(pos); }

        int line() {
            int line = 1;
            for (int i = 0; i < pos; i++) if (s.charAt(i) == '\n') line++;
            return line;
        }

        void skipWs() {
            while (!eof()) {
                char c = peek();
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        void expect(char c) {
            if (eof() || peek() != c) {
                throw new IllegalArgumentException("JSON 解析失败: 第 " + line() + " 行期望 '" + c + "'");
            }
            pos++;
        }

        Object readValue() {
            skipWs();
            if (eof()) throw new IllegalArgumentException("JSON 解析失败: 提前结束");
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
            if (!eof() && peek() == '}') { pos++; return m; }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object val = readValue();
                m.put(key, val);
                skipWs();
                if (!eof() && peek() == ',') { pos++; continue; }
                expect('}');
                return m;
            }
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            expect('[');
            skipWs();
            if (!eof() && peek() == ']') { pos++; return list; }
            while (true) {
                list.add(readValue());
                skipWs();
                if (!eof() && peek() == ',') { pos++; continue; }
                expect(']');
                return list;
            }
        }

        Boolean readBool() {
            if (s.startsWith("true", pos)) { pos += 4; return Boolean.TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return Boolean.FALSE; }
            throw new IllegalArgumentException("JSON 解析失败: 第 " + line() + " 行非法字面量");
        }

        Object readNull() {
            if (s.startsWith("null", pos)) { pos += 4; return null; }
            throw new IllegalArgumentException("JSON 解析失败: 第 " + line() + " 行非法字面量");
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw new IllegalArgumentException("JSON 解析失败: 字符串未闭合");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw new IllegalArgumentException("JSON 解析失败: 非法转义");
                    char e = s.charAt(pos++);
                    switch (e) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'u':
                            if (pos + 4 > s.length()) throw new IllegalArgumentException("JSON 解析失败: 非法 \\u 转义");
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                            break;
                        default:
                            throw new IllegalArgumentException("JSON 解析失败: 非法转义 \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Number readNumber() {
            int start = pos;
            if (!eof() && peek() == '-') pos++;
            readDigits();
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
            String tok = s.substring(start, pos);
            if (tok.isEmpty() || "-".equals(tok)) {
                throw new IllegalArgumentException("JSON 解析失败: 第 " + line() + " 行非法数字");
            }
            if (isDouble) return Double.valueOf(tok);
            try {
                return Long.valueOf(Long.parseLong(tok));
            } catch (NumberFormatException ex) {
                return Double.valueOf(tok);
            }
        }

        void readDigits() {
            int n = 0;
            while (!eof() && Character.isDigit(peek())) { pos++; n++; }
            if (n == 0) throw new IllegalArgumentException("JSON 解析失败: 第 " + line() + " 行非法数字");
        }
    }
}
