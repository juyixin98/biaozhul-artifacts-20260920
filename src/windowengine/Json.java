package windowengine;

import java.io.IOException;
import java.io.StringWriter;
import java.io.Writer;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * 极简 JSON 门面：手写的递归下降解析器 + 输出器，无任何第三方依赖。
 *
 * 数据模型映射：
 *   object -> LinkedHashMap&lt;String,Object&gt;（保留键顺序）
 *   array  -> ArrayList&lt;Object&gt;
 *   string -> String
 *   number -> Long（本引擎只接受整数；小数 / 指数写法一律 INVALID_JSON）
 *   true/false -> Boolean
 *   null -> Java null
 */
public final class Json {

    private Json() {
    }

    // ---------- 读取 ----------

    public static Object parse(String text) {
        JsonParser parser = new JsonParser(text);
        parser.skipWhitespace();
        Object value = parser.readValue();
        parser.skipWhitespace();
        if (!parser.eof()) {
            throw parser.error("JSON 结束后仍有多余字符");
        }
        return value;
    }

    public static Object parseFile(Path path) {
        try {
            return parse(Files.readString(path, StandardCharsets.UTF_8));
        } catch (IOException e) {
            throw new EngineException(ErrorCode.IO_ERROR,
                    "无法读取文件: " + path + " (" + e.getMessage() + ")", e);
        }
    }

    // ---------- 输出 ----------

    public static String write(Object value) {
        StringWriter sw = new StringWriter();
        new JsonWriter(sw, false).writeValue(value);
        return sw.toString();
    }

    public static String writePretty(Object value) {
        StringWriter sw = new StringWriter();
        new JsonWriter(sw, true).writeValue(value);
        sw.write('\n');
        return sw.toString();
    }

    public static void writeFile(Path path, Object value, boolean pretty) {
        try {
            if (path.getParent() != null) {
                Files.createDirectories(path.getParent());
            }
            String text = pretty ? writePretty(value) : write(value);
            Files.writeString(path, text, StandardCharsets.UTF_8);
        } catch (IOException e) {
            throw new EngineException(ErrorCode.IO_ERROR,
                    "无法写入文件: " + path + " (" + e.getMessage() + ")", e);
        }
    }

    // ---------- 便捷取值（带友好错误） ----------

    @SuppressWarnings("unchecked")
    public static Map<String, Object> asObject(Object value, String what) {
        if (!(value instanceof Map)) {
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    what + " 必须是 JSON 对象");
        }
        return (Map<String, Object>) value;
    }

    @SuppressWarnings("unchecked")
    public static List<Object> asArray(Object value, String what) {
        if (!(value instanceof List)) {
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    what + " 必须是 JSON 数组");
        }
        return (List<Object>) value;
    }

    public static String requireString(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (!(v instanceof String s)) {
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    "字段 '" + key + "' 必须是字符串");
        }
        return s;
    }

    public static String optionalString(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (v == null) {
            return null;
        }
        if (!(v instanceof String s)) {
            throw new EngineException(ErrorCode.INVALID_REQUEST,
                    "字段 '" + key + "' 必须是字符串");
        }
        return s;
    }

    public static long requireLong(Map<String, Object> obj, String key) {
        Object v = obj.get(key);
        if (v instanceof Long l) {
            return l;
        }
        if (v instanceof Number n) {
            // 兼容程序化构造请求时出现的 Integer/Short 等（真正经 JSON 解析进来的只有 Long）
            return n.longValue();
        }
        throw new EngineException(ErrorCode.INVALID_REQUEST,
                "字段 '" + key + "' 必须是整数");
    }
}

/** 递归下降 JSON 解析器。仅识别整数（拒绝小数/指数），适配本引擎的整数域。 */
final class JsonParser {

    private final String text;
    private int pos;

    JsonParser(String text) {
        this.text = text;
    }

    boolean eof() {
        return pos >= text.length();
    }

    EngineException error(String message) {
        return new EngineException(ErrorCode.INVALID_JSON,
                "JSON 解析错误（位置 " + pos + "）: " + message);
    }

    void skipWhitespace() {
        while (pos < text.length()) {
            char c = text.charAt(pos);
            if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                pos++;
            } else {
                break;
            }
        }
    }

    private char peek() {
        if (eof()) {
            throw error("意外结束");
        }
        return text.charAt(pos);
    }

    private char next() {
        char c = peek();
        pos++;
        return c;
    }

    private void expect(char c) {
        if (eof() || text.charAt(pos) != c) {
            throw error("期望 '" + c + "'");
        }
        pos++;
    }

    Object readValue() {
        skipWhitespace();
        if (eof()) {
            throw error("意外结束，缺少值");
        }
        char c = peek();
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
                throw error("非法值起始字符 '" + c + "'");
            }
        };
    }

    private Map<String, Object> readObject() {
        expect('{');
        Map<String, Object> map = new LinkedHashMap<>();
        skipWhitespace();
        if (peek() == '}') {
            pos++;
            return map;
        }
        while (true) {
            skipWhitespace();
            if (peek() != '"') {
                throw error("对象键必须是字符串");
            }
            String key = readString();
            skipWhitespace();
            expect(':');
            Object value = readValue();
            if (map.containsKey(key)) {
                throw error("对象内重复键: " + key);
            }
            map.put(key, value);
            skipWhitespace();
            char c = next();
            if (c == ',') {
                continue;
            }
            if (c == '}') {
                break;
            }
            throw error("对象成员后期望 ',' 或 '}'，实际为 '" + c + "'");
        }
        return map;
    }

    private List<Object> readArray() {
        expect('[');
        List<Object> list = new ArrayList<>();
        skipWhitespace();
        if (peek() == ']') {
            pos++;
            return list;
        }
        while (true) {
            list.add(readValue());
            skipWhitespace();
            char c = next();
            if (c == ',') {
                continue;
            }
            if (c == ']') {
                break;
            }
            throw error("数组元素后期望 ',' 或 ']'，实际为 '" + c + "'");
        }
        return list;
    }

    private String readString() {
        expect('"');
        StringBuilder sb = new StringBuilder();
        while (true) {
            if (eof()) {
                throw error("字符串未闭合");
            }
            char c = next();
            if (c == '"') {
                break;
            }
            if (c == '\\') {
                char esc = next();
                switch (esc) {
                    case '"' -> sb.append('"');
                    case '\\' -> sb.append('\\');
                    case '/' -> sb.append('/');
                    case 'b' -> sb.append('\b');
                    case 'f' -> sb.append('\f');
                    case 'n' -> sb.append('\n');
                    case 'r' -> sb.append('\r');
                    case 't' -> sb.append('\t');
                    case 'u' -> {
                        if (pos + 4 > text.length()) {
                            throw error("\\u 转义不完整");
                        }
                        String hex = text.substring(pos, pos + 4);
                        try {
                            sb.append((char) Integer.parseInt(hex, 16));
                        } catch (NumberFormatException e) {
                            throw error("非法 \\u 转义: " + hex);
                        }
                        pos += 4;
                    }
                    default -> throw error("非法转义字符 '\\" + esc + "'");
                }
            } else if (c < 0x20) {
                throw error("字符串内存在未转义控制字符");
            } else {
                sb.append(c);
            }
        }
        return sb.toString();
    }

    private Boolean readBoolean() {
        if (text.startsWith("true", pos)) {
            pos += 4;
            return Boolean.TRUE;
        }
        if (text.startsWith("false", pos)) {
            pos += 5;
            return Boolean.FALSE;
        }
        throw error("非法字面量");
    }

    private Object readNull() {
        if (text.startsWith("null", pos)) {
            pos += 4;
            return null;
        }
        throw error("非法字面量");
    }

    private Long readNumber() {
        int start = pos;
        if (peek() == '-') {
            pos++;
            if (eof()) {
                throw error("数字缺少尾数");
            }
        }
        char c = peek();
        if (c == '0') {
            pos++;
        } else if (c >= '1' && c <= '9') {
            while (!eof() && Character.isDigit(peek())) {
                pos++;
            }
        } else {
            throw error("非法数字");
        }
        // 引擎的整数域：显式拒绝小数、指数
        if (!eof()) {
            char tail = peek();
            if (tail == '.' || tail == 'e' || tail == 'E') {
                throw error("仅支持整数，不支持小数/指数写法");
            }
        }
        String token = text.substring(start, pos);
        try {
            return Long.parseLong(token);
        } catch (NumberFormatException e) {
            throw error("整数超出 long 范围: " + token);
        }
    }
}

/** JSON 输出器，支持紧凑与两空格缩进两种风格。 */
final class JsonWriter {

    private final Writer out;
    private final boolean pretty;
    private int indent;

    JsonWriter(Writer out, boolean pretty) {
        this.out = out;
        this.pretty = pretty;
    }

    void writeValue(Object value) {
        try {
            if (value == null) {
                out.write("null");
            } else if (value instanceof String s) {
                writeString(s);
            } else if (value instanceof Boolean b) {
                out.write(b.booleanValue() ? "true" : "false");
            } else if (value instanceof Long l) {
                out.write(l.toString());
            } else if (value instanceof Integer i) {
                out.write(Integer.toString(i));
            } else if (value instanceof Map<?, ?> map) {
                writeObject(map);
            } else if (value instanceof List<?> list) {
                writeArray(list);
            } else if (value instanceof Enum<?> e) {
                writeString(e.name());
            } else {
                throw new EngineException(ErrorCode.UNSUPPORTED,
                        "无法序列化为 JSON 的类型: " + value.getClass());
            }
        } catch (IOException e) {
            throw new EngineException(ErrorCode.IO_ERROR, "JSON 输出失败", e);
        }
    }

    @SuppressWarnings("unchecked")
    private void writeObject(Map<?, ?> map) throws IOException {
        if (map.isEmpty()) {
            out.write("{}");
            return;
        }
        out.write('{');
        indent++;
        boolean first = true;
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            if (!first) {
                out.write(',');
            }
            first = false;
            newline();
            writeString(String.valueOf(entry.getKey()));
            out.write(pretty ? ": " : ":");
            writeValue(entry.getValue());
        }
        indent--;
        newline();
        out.write('}');
    }

    private void writeArray(List<?> list) throws IOException {
        if (list.isEmpty()) {
            out.write("[]");
            return;
        }
        out.write('[');
        indent++;
        boolean first = true;
        for (Object item : list) {
            if (!first) {
                out.write(',');
            }
            first = false;
            newline();
            writeValue(item);
        }
        indent--;
        newline();
        out.write(']');
    }

    private void newline() throws IOException {
        if (pretty) {
            out.write('\n');
            out.write("  ".repeat(indent));
        }
    }

    private void writeString(String s) throws IOException {
        out.write('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"' -> out.write("\\\"");
                case '\\' -> out.write("\\\\");
                case '\b' -> out.write("\\b");
                case '\f' -> out.write("\\f");
                case '\n' -> out.write("\\n");
                case '\r' -> out.write("\\r");
                case '\t' -> out.write("\\t");
                default -> {
                    if (c < 0x20) {
                        out.write(String.format("\\u%04x", (int) c));
                    } else {
                        out.write(c);
                    }
                }
            }
        }
        out.write('"');
    }
}
