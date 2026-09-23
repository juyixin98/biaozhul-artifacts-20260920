package topk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析/序列化（仅支持本服务需要的子集：object/array/string/number/true/false/null）。
 * 数字解析为 Long 或 Double。不依赖任何第三方库。
 */
public final class Json {

    private Json() {}

    public static Object parse(String s) {
        Parser p = new Parser(s);
        p.skipWs();
        Object v = p.parseValue();
        p.skipWs();
        if (!p.atEnd()) throw new IllegalArgumentException("trailing characters at pos " + p.pos);
        return v;
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> parseObject(String s) {
        Object v = parse(s);
        if (!(v instanceof Map)) throw new IllegalArgumentException("expected JSON object");
        return (Map<String, Object>) v;
    }

    public static String stringify(Object v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb);
        return sb.toString();
    }

    /** 便捷构造有序对象。 */
    public static Map<String, Object> obj(Object... kv) {
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) m.put((String) kv[i], kv[i + 1]);
        return m;
    }

    private static void write(Object v, StringBuilder sb) {
        if (v == null) { sb.append("null"); return; }
        if (v instanceof String s) { writeString(s, sb); return; }
        if (v instanceof Number || v instanceof Boolean) { sb.append(v); return; }
        if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(String.valueOf(e.getKey()), sb);
                sb.append(':');
                write(e.getValue(), sb);
            }
            sb.append('}');
            return;
        }
        if (v instanceof Iterable<?> it) {
            sb.append('[');
            boolean first = true;
            for (Object o : it) {
                if (!first) sb.append(',');
                first = false;
                write(o, sb);
            }
            sb.append(']');
            return;
        }
        throw new IllegalArgumentException("cannot serialize " + v.getClass());
    }

    private static void writeString(String s, StringBuilder sb) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
                default -> {
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }

    private static final class Parser {
        final String s;
        int pos = 0;

        Parser(String s) { this.s = s; }

        boolean atEnd() { return pos >= s.length(); }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        char expect(char c) {
            if (atEnd() || s.charAt(pos) != c)
                throw new IllegalArgumentException("expected '" + c + "' at pos " + pos);
            return s.charAt(pos++);
        }

        Object parseValue() {
            skipWs();
            if (atEnd()) throw new IllegalArgumentException("unexpected end of input");
            char c = s.charAt(pos);
            return switch (c) {
                case '{' -> parseObject();
                case '[' -> parseArray();
                case '"' -> parseString();
                case 't' -> { expectLit("true"); yield Boolean.TRUE; }
                case 'f' -> { expectLit("false"); yield Boolean.FALSE; }
                case 'n' -> { expectLit("null"); yield null; }
                default -> parseNumber();
            };
        }

        void expectLit(String lit) {
            if (!s.startsWith(lit, pos))
                throw new IllegalArgumentException("expected " + lit + " at pos " + pos);
            pos += lit.length();
        }

        Map<String, Object> parseObject() {
            expect('{');
            Map<String, Object> m = new LinkedHashMap<>();
            skipWs();
            if (!atEnd() && s.charAt(pos) == '}') { pos++; return m; }
            while (true) {
                skipWs();
                String key = parseString();
                skipWs();
                expect(':');
                Object v = parseValue();
                m.put(key, v);
                skipWs();
                if (atEnd()) throw new IllegalArgumentException("unterminated object");
                char c = s.charAt(pos++);
                if (c == '}') break;
                if (c != ',') throw new IllegalArgumentException("expected ',' or '}' at pos " + (pos - 1));
            }
            return m;
        }

        List<Object> parseArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (!atEnd() && s.charAt(pos) == ']') { pos++; return list; }
            while (true) {
                list.add(parseValue());
                skipWs();
                if (atEnd()) throw new IllegalArgumentException("unterminated array");
                char c = s.charAt(pos++);
                if (c == ']') break;
                if (c != ',') throw new IllegalArgumentException("expected ',' or ']' at pos " + (pos - 1));
            }
            return list;
        }

        String parseString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) throw new IllegalArgumentException("unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') break;
                if (c == '\\') {
                    if (atEnd()) throw new IllegalArgumentException("bad escape");
                    char e = s.charAt(pos++);
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
                            if (pos + 4 > s.length()) throw new IllegalArgumentException("bad \\u escape");
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default -> throw new IllegalArgumentException("bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        Number parseNumber() {
            int start = pos;
            if (!atEnd() && (s.charAt(pos) == '-' || s.charAt(pos) == '+')) pos++;
            boolean isDouble = false;
            while (!atEnd()) {
                char c = s.charAt(pos);
                if (c >= '0' && c <= '9') pos++;
                else if (c == '.' || c == 'e' || c == 'E' || c == '-' || c == '+') { isDouble = true; pos++; }
                else break;
            }
            if (start == pos) throw new IllegalArgumentException("expected value at pos " + pos);
            String num = s.substring(start, pos);
            try {
                return isDouble ? Double.parseDouble(num) : Long.parseLong(num);
            } catch (NumberFormatException e) {
                throw new IllegalArgumentException("bad number '" + num + "'");
            }
        }
    }
}
