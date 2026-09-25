package booleansearch.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 最小 JSON 支持：解析 + 序列化，零外部依赖。
 *
 * <p>解析得到的对象模型只用到 JDK 类型：
 * {@link LinkedHashMap}（对象）、{@link ArrayList}（数组）、
 * {@link String}、{@link Long}、{@link Double}、{@link Boolean}、null。
 *
 * <p>解析错误抛 {@link JsonParseException}，携带字符位置（0 基），
 * 与查询解析错误一样精确保留位置。
 */
public final class Json {

    private Json() {
    }

    // ---------- 序列化 ----------

    public static String stringify(Object value) {
        StringBuilder sb = new StringBuilder();
        writeValue(sb, value);
        return sb.toString();
    }

    public static String pretty(Object value) {
        StringBuilder sb = new StringBuilder();
        writePretty(sb, value, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void writeValue(StringBuilder sb, Object value) {
        if (value == null) {
            sb.append("null");
        } else if (value instanceof String s) {
            writeString(sb, s);
        } else if (value instanceof Boolean b) {
            sb.append(b.booleanValue());
        } else if (value instanceof Integer i) {
            sb.append(i.intValue());
        } else if (value instanceof Long l) {
            sb.append(l.longValue());
        } else if (value instanceof Double d) {
            sb.append(d.doubleValue());
        } else if (value instanceof Number n) {
            sb.append(n.toString());
        } else if (value instanceof Map<?, ?> map) {
            writeMap(sb, map);
        } else if (value.getClass().isRecord()) {
            // record：按组件名序列化为 JSON 对象
            java.util.LinkedHashMap<String, Object> recordFields = new java.util.LinkedHashMap<>();
            for (java.lang.reflect.RecordComponent rc : value.getClass().getRecordComponents()) {
                try {
                    rc.getAccessor().setAccessible(true);
                    recordFields.put(rc.getName(), rc.getAccessor().invoke(value));
                } catch (ReflectiveOperationException e) {
                    throw new IllegalStateException("无法序列化 record 组件 " + rc.getName(), e);
                }
            }
            writeMap(sb, recordFields);
        } else if (value instanceof Iterable<?> iterable) {
            sb.append('[');
            boolean first = true;
            for (Object item : iterable) {
                if (!first) {
                    sb.append(',');
                }
                first = false;
                writeValue(sb, item);
            }
            sb.append(']');
        } else {
            writeString(sb, value.toString());
        }
    }

    private static void writePretty(StringBuilder sb, Object value, int indent) {
        if (value != null && value.getClass().isRecord() && !(value instanceof Map)) {
            java.util.LinkedHashMap<String, Object> recordFields = new java.util.LinkedHashMap<>();
            for (java.lang.reflect.RecordComponent rc : value.getClass().getRecordComponents()) {
                try {
                    rc.getAccessor().setAccessible(true);
                    recordFields.put(rc.getName(), rc.getAccessor().invoke(value));
                } catch (ReflectiveOperationException e) {
                    throw new IllegalStateException("无法序列化 record 组件 " + rc.getName(), e);
                }
            }
            writePretty(sb, recordFields, indent);
        } else if (value instanceof Map<?, ?> map && !map.isEmpty()) {
            sb.append("{\n");
            boolean first = true;
            for (Map.Entry<?, ?> e : map.entrySet()) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                pad(sb, indent + 1);
                writeString(sb, String.valueOf(e.getKey()));
                sb.append(": ");
                writePretty(sb, e.getValue(), indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append('}');
        } else if (value instanceof Iterable<?> iterable && !isEmpty(iterable)) {
            sb.append("[\n");
            boolean first = true;
            for (Object item : iterable) {
                if (!first) {
                    sb.append(",\n");
                }
                first = false;
                pad(sb, indent + 1);
                writePretty(sb, item, indent + 1);
            }
            sb.append('\n');
            pad(sb, indent);
            sb.append(']');
        } else {
            writeValue(sb, value);
        }
    }

    private static boolean isEmpty(Iterable<?> iterable) {
        return iterable instanceof java.util.Collection<?> c ? c.isEmpty() : !iterable.iterator().hasNext();
    }

    private static void pad(StringBuilder sb, int indent) {
        sb.append("  ".repeat(indent));
    }

    private static void writeMap(StringBuilder sb, Map<?, ?> map) {
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

    // ---------- 解析 ----------

    public static Object parse(String text) {
        Parser parser = new Parser(text);
        parser.skipWhitespace();
        Object value = parser.readValue();
        parser.skipWhitespace();
        if (!parser.atEnd()) {
            throw new JsonParseException("JSON 解析结束后仍有多余字符", parser.pos);
        }
        return value;
    }

    /** 取字符串字段；缺失或类型不符时抛带位置信息的异常由上层转 400。 */
    @SuppressWarnings("unchecked")
    public static String getString(Map<String, Object> obj, String key) {
        Object value = obj.get(key);
        if (value instanceof String s) {
            return s;
        }
        if (value == null) {
            throw new BadRequestException("缺少字段 \"" + key + "\"");
        }
        throw new BadRequestException("字段 \"" + key + "\" 必须是字符串");
    }

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object value) {
        if (value instanceof Map<?, ?> map) {
            return (Map<String, Object>) map;
        }
        throw new BadRequestException("请求体必须是 JSON 对象");
    }

    /** 输入不合法（语义层），服务层映射为 HTTP 400。 */
    public static class BadRequestException extends RuntimeException {
        public BadRequestException(String message) {
            super(message);
        }
    }

    private static final class Parser {
        private final String text;
        private int pos;

        private Parser(String text) {
            this.text = text == null ? "" : text;
        }

        boolean atEnd() {
            return pos >= text.length();
        }

        void skipWhitespace() {
            while (pos < text.length() && Character.isWhitespace(text.charAt(pos))) {
                pos++;
            }
        }

        Object readValue() {
            skipWhitespace();
            if (atEnd()) {
                throw new JsonParseException("JSON 不完整，意外结束", pos);
            }
            char c = text.charAt(pos);
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> readString();
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> {
                    if (c == '-' || Character.isDigit(c)) {
                        yield readNumber();
                    }
                    throw new JsonParseException("无法识别的 JSON 值起始字符 '" + c + "'", pos);
                }
            };
        }

        Map<String, Object> readObject() {
            Map<String, Object> map = new LinkedHashMap<>();
            expect('{');
            skipWhitespace();
            if (consume('}')) {
                return map;
            }
            while (true) {
                skipWhitespace();
                if (atEnd() || text.charAt(pos) != '"') {
                    throw new JsonParseException("对象键必须是字符串", pos);
                }
                String key = readString();
                skipWhitespace();
                if (atEnd() || text.charAt(pos) != ':') {
                    throw new JsonParseException("对象键后缺少 ':'", pos);
                }
                pos++;
                Object value = readValue();
                map.put(key, value);
                skipWhitespace();
                if (atEnd()) {
                    throw new JsonParseException("对象缺少右花括号 '}'", pos);
                }
                char c = text.charAt(pos);
                if (c == ',') {
                    pos++;
                    continue;
                }
                if (c == '}') {
                    pos++;
                    return map;
                }
                throw new JsonParseException("对象成员之间需要 ',' 或 '}'，实际为 '" + c + "'", pos);
            }
        }

        List<Object> readArray() {
            List<Object> list = new ArrayList<>();
            expect('[');
            skipWhitespace();
            if (consume(']')) {
                return list;
            }
            while (true) {
                list.add(readValue());
                skipWhitespace();
                if (atEnd()) {
                    throw new JsonParseException("数组缺少右方括号 ']'", pos);
                }
                char c = text.charAt(pos);
                if (c == ',') {
                    pos++;
                    continue;
                }
                if (c == ']') {
                    pos++;
                    return list;
                }
                throw new JsonParseException("数组元素之间需要 ',' 或 ']'，实际为 '" + c + "'", pos);
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (pos < text.length()) {
                char c = text.charAt(pos++);
                if (c == '"') {
                    return sb.toString();
                }
                if (c == '\\') {
                    if (pos >= text.length()) {
                        throw new JsonParseException("字符串转义不完整", pos);
                    }
                    char esc = text.charAt(pos++);
                    switch (esc) {
                        case '"' -> sb.append('"');
                        case '\\' -> sb.append('\\');
                        case '/' -> sb.append('/');
                        case 'n' -> sb.append('\n');
                        case 't' -> sb.append('\t');
                        case 'r' -> sb.append('\r');
                        case 'b' -> sb.append('\b');
                        case 'f' -> sb.append('\f');
                        case 'u' -> {
                            if (pos + 4 > text.length()) {
                                throw new JsonParseException("\\u 转义需要 4 位十六进制数", pos);
                            }
                            String hex = text.substring(pos, pos + 4);
                            try {
                                sb.append((char) Integer.parseInt(hex, 16));
                            } catch (NumberFormatException e) {
                                throw new JsonParseException("非法的 \\u 转义: " + hex, pos);
                            }
                            pos += 4;
                        }
                        default -> throw new JsonParseException("非法的转义字符 '\\" + esc + "'",
                                pos - 1);
                    }
                } else if (c < 0x20) {
                    throw new JsonParseException("字符串中存在未转义的控制字符", pos - 1);
                } else {
                    sb.append(c);
                }
            }
            throw new JsonParseException("字符串缺少右引号", pos);
        }

        Boolean readBoolean() {
            if (text.startsWith("true", pos)) {
                pos += 4;
                return Boolean.TRUE;
            }
            if (text.startsWith("false", pos)) {
                pos += 5;
                return Boolean.FALSE;
            }
            throw new JsonParseException("非法的字面量（应为 true/false/null）", pos);
        }

        Object readNull() {
            if (text.startsWith("null", pos)) {
                pos += 4;
                return null;
            }
            throw new JsonParseException("非法的字面量（应为 null）", pos);
        }

        Object readNumber() {
            int start = pos;
            if (pos < text.length() && text.charAt(pos) == '-') {
                pos++;
            }
            readDigits();
            boolean isDouble = false;
            if (pos < text.length() && text.charAt(pos) == '.') {
                isDouble = true;
                pos++;
                readDigits();
            }
            if (pos < text.length() && (text.charAt(pos) == 'e' || text.charAt(pos) == 'E')) {
                isDouble = true;
                pos++;
                if (pos < text.length() && (text.charAt(pos) == '+' || text.charAt(pos) == '-')) {
                    pos++;
                }
                readDigits();
            }
            String number = text.substring(start, pos);
            try {
                // 注意：这里不能写成三元表达式，否则 Double/Long 两个包装分支会被
                // 统一拓宽为 double，整数也会被装箱成 Double。
                if (isDouble) {
                    return Double.valueOf(number);
                }
                return Long.valueOf(number);
            } catch (NumberFormatException e) {
                throw new JsonParseException("非法数字: " + number, start);
            }
        }

        void readDigits() {
            int start = pos;
            while (pos < text.length() && Character.isDigit(text.charAt(pos))) {
                pos++;
            }
            if (start == pos) {
                throw new JsonParseException("数字缺少数位", pos);
            }
        }

        void expect(char expected) {
            if (atEnd() || text.charAt(pos) != expected) {
                throw new JsonParseException("期望 '" + expected + "'", pos);
            }
            pos++;
        }

        boolean consume(char expected) {
            if (!atEnd() && text.charAt(pos) == expected) {
                pos++;
                return true;
            }
            return false;
        }
    }
}
