package intervalindex;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 解析/序列化（仅支持本服务需要的子集，无任何第三方依赖）。
 *
 * <p>支持类型：object、array、string、number（统一解析为 long，拒绝小数/指数写法）、
 * true/false/null。数字刻意只接受整数（端点必须是整数）。
 */
final class Json {

    private Json() {}

    @SuppressWarnings("serial")
    static final class ParseException extends RuntimeException {
        ParseException(String message) {
            super(message);
        }
    }

    @SuppressWarnings("unchecked")
    static Map<String, Object> parseObject(String text) {
        Object value = parse(text);
        if (!(value instanceof Map)) {
            throw new ParseException("request body must be a JSON object");
        }
        return (Map<String, Object>) value;
    }

    static Object parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Object value = p.readValue();
        p.skipWs();
        if (!p.atEnd()) {
            throw new ParseException("unexpected trailing content at position " + p.pos);
        }
        return value;
    }

    /** 从对象取 long 字段；缺失或类型不对时给出清晰错误。 */
    static long requireLong(Map<String, Object> obj, String field) {
        Object v = obj.get(field);
        if (v == null) {
            throw new ParseException("missing required integer field \"" + field + "\"");
        }
        if (!(v instanceof Long)) {
            throw new ParseException("field \"" + field + "\" must be an integer");
        }
        return (Long) v;
    }

    /** 取可选 long 字段；不存在返回默认值；存在但类型错误则报错。 */
    static long optionalLong(Map<String, Object> obj, String field, long def) {
        if (!obj.containsKey(field)) {
            return def;
        }
        return requireLong(obj, field);
    }

    /** 序列化为紧凑 JSON（字符串做完整转义）。 */
    static String write(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    static Map<String, Object> object(Object... kv) {
        if (kv.length % 2 != 0) {
            throw new IllegalArgumentException("key/value args must be paired");
        }
        Map<String, Object> m = new LinkedHashMap<>();
        for (int i = 0; i < kv.length; i += 2) {
            m.put((String) kv[i], kv[i + 1]);
        }
        return m;
    }

    @SuppressWarnings("unchecked")
    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(sb, s);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Number n) {
            sb.append(n.toString());
        } else if (value instanceof Map<?, ?> map) {
            sb.append('{');
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(':');
                writeValue(sb, e.getValue());
            }
            sb.append('}');
        } else if (value instanceof List<?> list) {
            sb.append('[');
            boolean first = true;
            for (Object item : list) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else {
            throw new IllegalArgumentException("cannot serialize " + value.getClass());
        }
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

    // ------------------------------------------------------------------
    // 递归下降解析
    // ------------------------------------------------------------------

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean atEnd() {
            return pos >= s.length();
        }

        void skipWs() {
            while (pos < s.length() && Character.isWhitespace(s.charAt(pos))) {
                pos++;
            }
        }

        Object readValue() {
            skipWs();
            if (atEnd()) {
                throw new ParseException("unexpected end of JSON");
            }
            char c = s.charAt(pos);
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> {
                    if (c == '-' || (c >= '0' && c <= '9')) {
                        yield readNumber();
                    }
                    throw new ParseException("unexpected character '" + c + "' at position " + pos);
                }
            };
        }

        Map<String, Object> readObject() {
            expect('{');
            Map<String, Object> map = new LinkedHashMap<>();
            skipWs();
            if (peek() == '}') {
                pos++;
                return map;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                Object value = readValue();
                map.put(key, value);
                skipWs();
                char c = next();
                if (c == '}') {
                    return map;
                }
                if (c != ',') {
                    throw new ParseException("expected ',' or '}' at position " + (pos - 1));
                }
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
                if (c == ']') {
                    return list;
                }
                if (c != ',') {
                    throw new ParseException("expected ',' or ']' at position " + (pos - 1));
                }
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (atEnd()) {
                    throw new ParseException("unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (atEnd()) {
                        throw new ParseException("unterminated escape");
                    }
                    char e = s.charAt(pos++);
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
                            if (pos + 4 > s.length()) {
                                throw new ParseException("bad \\u escape at position " + pos);
                            }
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                        }
                        default -> throw new ParseException("bad escape '\\" + e + "'");
                    }
                } else {
                    if (c < 0x20) {
                        throw new ParseException("unescaped control character in string");
                    }
                    sb.append(c);
                }
            }
        }

        Object readNumber() {
            int start = pos;
            if (peek() == '-') {
                pos++;
            }
            if (atEnd() || !Character.isDigit(s.charAt(pos))) {
                throw new ParseException("bad number at position " + start);
            }
            while (!atEnd() && Character.isDigit(s.charAt(pos))) {
                pos++;
            }
            // 本服务端点必须是整数：拒绝小数、指数以及任何后缀
            if (!atEnd()) {
                char c = s.charAt(pos);
                if (c == '.' || c == 'e' || c == 'E') {
                    throw new ParseException("only integer numbers are supported; got number at position " + start);
                }
            }
            String digits = s.substring(start, pos);
            try {
                return Long.parseLong(digits);
            } catch (NumberFormatException ex) {
                throw new ParseException("integer out of 64-bit range at position " + start);
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
            throw new ParseException("invalid literal at position " + pos);
        }

        Object readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new ParseException("invalid literal at position " + pos);
        }

        char peek() {
            if (atEnd()) {
                throw new ParseException("unexpected end of JSON");
            }
            return s.charAt(pos);
        }

        char next() {
            if (atEnd()) {
                throw new ParseException("unexpected end of JSON");
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            char actual = next();
            if (actual != c) {
                throw new ParseException("expected '" + c + "' but got '" + actual + "' at position " + (pos - 1));
            }
        }
    }
}
