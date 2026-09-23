package ij;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析/序列化，零第三方依赖。
 * 支持类型：Map(LinkedHashMap)、List、String、Long、Double、Boolean、null。
 * 整数按 long 解析（超出 long 范围才退化为 double），保证事件时间戳精度。
 */
final class Json {

    static final class JsonException extends RuntimeException {
        JsonException(String message) {
            super(message);
        }
    }

    private Json() {
    }

    // ---------------- 解析 ----------------

    @SuppressWarnings("unchecked")
    static Map<String, Object> parseObject(String text) {
        Object v = parse(text);
        if (!(v instanceof Map)) {
            throw new JsonException("expected JSON object");
        }
        return (Map<String, Object>) v;
    }

    static Object parse(String text) {
        Parser p = new Parser(text == null ? "" : text);
        p.skipWs();
        Object v = p.readValue();
        p.skipWs();
        if (p.pos != p.s.length()) {
            throw new JsonException("trailing characters at position " + p.pos);
        }
        return v;
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Object readValue() {
            skipWs();
            if (pos >= s.length()) {
                throw new JsonException("unexpected end of input");
            }
            char c = s.charAt(pos);
            switch (c) {
                case '{':
                    return readObject();
                case '[':
                    return readArray();
                case '"':
                    return readString();
                case 't':
                case 'f':
                    return readBoolean();
                case 'n':
                    return readNull();
                default:
                    return readNumber();
            }
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> m = new LinkedHashMap<>();
            skipWs();
            if (peek() == '}') {
                pos++;
                return m;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object val = readValue();
                m.put(key, val);
                skipWs();
                char c = next();
                if (c == ',') {
                    continue;
                }
                if (c == '}') {
                    return m;
                }
                throw new JsonException("expected ',' or '}' at position " + pos);
            }
        }

        List<Object> readArray() {
            expect('[');
            List<Object> list = new ArrayList<>();
            skipWs();
            if (peek() == ']') {
                pos++;
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWs();
                char c = next();
                if (c == ',') {
                    continue;
                }
                if (c == ']') {
                    return list;
                }
                throw new JsonException("expected ',' or ']' at position " + pos);
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (pos >= s.length()) {
                    throw new JsonException("unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    char e = next();
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
                            if (pos + 4 > s.length()) {
                                throw new JsonException("bad unicode escape");
                            }
                            String hex = s.substring(pos, pos + 4);
                            pos += 4;
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException ex) {
                                throw new JsonException("bad unicode escape: " + hex);
                            }
                            break;
                        default:
                            throw new JsonException("bad escape: \\" + e);
                    }
                } else if (c < 0x20) {
                    throw new JsonException("unescaped control character in string");
                } else {
                    sb.append(c);
                }
            }
        }

        Boolean readBoolean() {
            if (s.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (s.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new JsonException("invalid literal at position " + pos);
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new JsonException("invalid literal at position " + pos);
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') {
                pos++;
            }
            boolean isDouble = false;
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c >= '0' && c <= '9') {
                    pos++;
                } else if (c == '.' || c == 'e' || c == 'E' || c == '+' || c == '-') {
                    isDouble = true;
                    pos++;
                } else {
                    break;
                }
            }
            String token = s.substring(start, pos);
            if (token.isEmpty() || token.equals("-")) {
                throw new JsonException("invalid number at position " + start);
            }
            if (isDouble) {
                try {
                    return Double.parseDouble(token);
                } catch (NumberFormatException ex) {
                    throw new JsonException("invalid number: " + token);
                }
            }
            try {
                return Long.parseLong(token);
            } catch (NumberFormatException ex) {
                try {
                    return Double.parseDouble(token);
                } catch (NumberFormatException ex2) {
                    throw new JsonException("invalid number: " + token);
                }
            }
        }

        char peek() {
            if (pos >= s.length()) {
                throw new JsonException("unexpected end of input");
            }
            return s.charAt(pos);
        }

        char next() {
            if (pos >= s.length()) {
                throw new JsonException("unexpected end of input");
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            if (pos >= s.length() || s.charAt(pos) != c) {
                throw new JsonException("expected '" + c + "' at position " + pos);
            }
            pos++;
        }
    }

    // ---------------- 类型化取值辅助 ----------------

    static Map<String, Object> asObject(Object v, String field) {
        if (v instanceof Map) {
            @SuppressWarnings("unchecked")
            Map<String, Object> m = (Map<String, Object>) v;
            return m;
        }
        throw new JsonException(field + " must be an object");
    }

    @SuppressWarnings("unchecked")
    static List<Object> asArray(Object v, String field) {
        if (v instanceof List) {
            return (List<Object>) v;
        }
        throw new JsonException(field + " must be an array");
    }

    static String requireString(Map<String, Object> m, String field) {
        Object v = m.get(field);
        if (!(v instanceof String)) {
            throw new JsonException("missing or non-string field: " + field);
        }
        return (String) v;
    }

    static String optionalString(Map<String, Object> m, String field) {
        Object v = m.get(field);
        if (v == null) {
            return null;
        }
        if (!(v instanceof String)) {
            throw new JsonException(field + " must be a string");
        }
        return (String) v;
    }

    static long requireLong(Map<String, Object> m, String field) {
        Object v = m.get(field);
        if (v instanceof Number) {
            return ((Number) v).longValue();
        }
        throw new JsonException("missing or non-integer field: " + field);
    }

    static long optionalLong(Map<String, Object> m, String field, long dflt) {
        Object v = m.get(field);
        if (v == null) {
            return dflt;
        }
        if (v instanceof Number) {
            return ((Number) v).longValue();
        }
        throw new JsonException(field + " must be an integer");
    }

    // ---------------- 序列化 ----------------

    static String write(Object v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    private static void writeTo(StringBuilder sb, Object v) {
        if (v == null) {
            sb.append("null");
        } else if (v instanceof String) {
            writeString(sb, (String) v);
        } else if (v instanceof Boolean) {
            sb.append(v.toString());
        } else if (v instanceof Long || v instanceof Integer) {
            sb.append(v.toString());
        } else if (v instanceof Number) {
            double d = ((Number) v).doubleValue();
            if (d == Math.floor(d) && !Double.isInfinite(d) && Math.abs(d) < 1e18) {
                sb.append((long) d);
            } else {
                sb.append(v.toString());
            }
        } else if (v instanceof Map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : ((Map<?, ?>) v).entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeTo(sb, e.getValue());
            }
            sb.append('}');
        } else if (v instanceof List) {
            sb.append('[');
            boolean first = true;
            for (Object item : (List<?>) v) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeTo(sb, item);
            }
            sb.append(']');
        } else {
            writeString(sb, String.valueOf(v));
        }
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
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                case '\b':
                    sb.append("\\b");
                    break;
                case '\f':
                    sb.append("\\f");
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
