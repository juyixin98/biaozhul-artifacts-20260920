package boolsearch.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 迷你 JSON 解析/序列化（零依赖）。
 * 值类型：Map<String,Object> / List<Object> / String / Double / Long / Boolean / null。
 * 解析错误抛出 JsonException 并带字符位置。
 */
public final class Json {
    private Json() {}

    public static final class JsonException extends Exception {
        private final int position;
        public JsonException(String message, int position) {
            super(message);
            this.position = position;
        }
        public int position() { return position; }
    }

    // ---------- 解析 ----------

    public static Object parse(String s) throws JsonException {
        Parser p = new Parser(s);
        p.skipWs();
        Object v = p.parseValue();
        p.skipWs();
        if (p.i != p.n) throw new JsonException("JSON 末尾存在多余内容", p.i);
        return v;
    }

    private static final class Parser {
        final String s; final int n; int i;
        Parser(String s) { this.s = s; this.n = s.length(); }

        void skipWs() {
            while (i < n && (s.charAt(i) == ' ' || s.charAt(i) == '\t'
                    || s.charAt(i) == '\n' || s.charAt(i) == '\r')) i++;
        }

        Object parseValue() throws JsonException {
            if (i >= n) throw new JsonException("意外的输入结束", i);
            char c = s.charAt(i);
            return switch (c) {
                case '{' -> parseObject();
                case '[' -> parseArray();
                case '"' -> parseString();
                case 't' -> { expectLit("true"); yield Boolean.TRUE; }
                case 'f' -> { expectLit("false"); yield Boolean.FALSE; }
                case 'n' -> { expectLit("null"); yield null; }
                default -> {
                    if (c == '-' || Character.isDigit(c)) yield parseNumber();
                    throw new JsonException("意外的字符 '" + c + "'", i);
                }
            };
        }

        void expectLit(String lit) throws JsonException {
            if (!s.startsWith(lit, i)) throw new JsonException("无效的字面量", i);
            i += lit.length();
        }

        Map<String, Object> parseObject() throws JsonException {
            Map<String, Object> m = new LinkedHashMap<>();
            i++; // '{'
            skipWs();
            if (i < n && s.charAt(i) == '}') { i++; return m; }
            while (true) {
                skipWs();
                if (i >= n || s.charAt(i) != '"') throw new JsonException("期望对象键（字符串）", i);
                String key = parseString();
                skipWs();
                if (i >= n || s.charAt(i) != ':') throw new JsonException("期望 ':'", i);
                i++;
                skipWs();
                m.put(key, parseValue());
                skipWs();
                if (i >= n) throw new JsonException("对象未闭合", i);
                char c = s.charAt(i);
                if (c == ',') { i++; continue; }
                if (c == '}') { i++; return m; }
                throw new JsonException("期望 ',' 或 '}'", i);
            }
        }

        List<Object> parseArray() throws JsonException {
            List<Object> list = new ArrayList<>();
            i++; // '['
            skipWs();
            if (i < n && s.charAt(i) == ']') { i++; return list; }
            while (true) {
                skipWs();
                list.add(parseValue());
                skipWs();
                if (i >= n) throw new JsonException("数组未闭合", i);
                char c = s.charAt(i);
                if (c == ',') { i++; continue; }
                if (c == ']') { i++; return list; }
                throw new JsonException("期望 ',' 或 ']'", i);
            }
        }

        String parseString() throws JsonException {
            StringBuilder sb = new StringBuilder();
            i++; // '"'
            while (true) {
                if (i >= n) throw new JsonException("字符串未闭合", i);
                char c = s.charAt(i++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (i >= n) throw new JsonException("转义序列不完整", i);
                    char e = s.charAt(i++);
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
                            if (i + 4 > n) throw new JsonException("\\u 转义不完整", i);
                            try {
                                sb.append((char) Integer.parseInt(s.substring(i, i + 4), 16));
                            } catch (NumberFormatException ex) {
                                throw new JsonException("无效的 \\u 转义", i);
                            }
                            i += 4;
                        }
                        default -> throw new JsonException("无效的转义 '\\" + e + "'", i - 1);
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        Number parseNumber() throws JsonException {
            int start = i;
            if (i < n && s.charAt(i) == '-') i++;
            while (i < n && Character.isDigit(s.charAt(i))) i++;
            boolean isDouble = false;
            if (i < n && s.charAt(i) == '.') {
                isDouble = true; i++;
                while (i < n && Character.isDigit(s.charAt(i))) i++;
            }
            if (i < n && (s.charAt(i) == 'e' || s.charAt(i) == 'E')) {
                isDouble = true; i++;
                if (i < n && (s.charAt(i) == '+' || s.charAt(i) == '-')) i++;
                while (i < n && Character.isDigit(s.charAt(i))) i++;
            }
            String num = s.substring(start, i);
            if (num.isEmpty() || num.equals("-")) throw new JsonException("无效的数字", start);
            try {
                return isDouble ? (Number) Double.parseDouble(num) : (Number) Long.parseLong(num);
            } catch (NumberFormatException e) {
                throw new JsonException("无效的数字 '" + num + "'", start);
            }
        }
    }

    // ---------- 序列化 ----------

    public static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, v);
        return sb.toString();
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object v) {
        if (v == null) { sb.append("null"); return; }
        if (v instanceof String s) { writeString(sb, s); return; }
        if (v instanceof Boolean || v instanceof Number) { sb.append(v); return; }
        if (v instanceof Map<?, ?> m) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : m.entrySet()) {
                if (!first) sb.append(',');
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
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
                writeValue(sb, o);
            }
            sb.append(']');
            return;
        }
        throw new IllegalArgumentException("不支持的 JSON 值类型: " + v.getClass());
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
                default -> {
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
                }
            }
        }
        sb.append('"');
    }
}
