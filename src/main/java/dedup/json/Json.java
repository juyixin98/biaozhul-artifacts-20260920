package dedup.json;

import java.io.IOException;
import java.io.Reader;
import java.math.BigDecimal;
import java.nio.charset.StandardCharsets;
import java.security.MessageDigest;
import java.security.NoSuchAlgorithmException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.Map;

/**
 * 极简、零依赖的 JSON 解析 / 序列化工具（仅 JDK）。
 *
 * <p>支持 object / array / string / number / true / false / null。
 * 对象保持键的插入顺序（{@link LinkedHashMap}），数字以 {@link BigDecimal} 保留精度。
 *
 * <p>另外提供去重所需的“规范化序列化”{@link #canonical(Value)}：对象按键排序、
 * 数字去除多余尾零，从而使键序不同 / 数字写法不同（1 与 1.00）的等价载荷得到相同哈希。
 */
public final class Json {

    private Json() {}

    // ---------------------------------------------------------------------
    // 值模型
    // ---------------------------------------------------------------------

    public sealed interface Value permits Obj, Arr, Str, Num, Bool, Nul {}

    /** JSON 对象，保持插入顺序。 */
    public static final class Obj extends LinkedHashMap<String, Value> implements Value {
        public Obj() {}
        public Obj(int initialCapacity) { super(initialCapacity); }

        public String getStr(String key) {
            Value v = get(key);
            return v instanceof Str s ? s.value() : null;
        }
    }

    /** JSON 数组。 */
    public static final class Arr extends ArrayList<Value> implements Value {}

    public record Str(String value) implements Value {}

    public record Num(BigDecimal value) implements Value {
        public static Num of(long v) { return new Num(BigDecimal.valueOf(v)); }
    }

    public record Bool(boolean value) implements Value {
        public static final Bool TRUE = new Bool(true);
        public static final Bool FALSE = new Bool(false);
    }

    public enum Nul implements Value { INSTANCE }

    /** 解析错误。 */
    public static final class JsonException extends RuntimeException {
        public JsonException(String message) { super(message); }
        public JsonException(String message, Throwable cause) { super(message, cause); }
    }

    // ---------------------------------------------------------------------
    // 解析
    // ---------------------------------------------------------------------

    public static Value parse(String input) {
        Parser p = new Parser(input);
        Value v = p.readValue();
        p.skipWhitespace();
        if (!p.eof()) {
            throw new JsonException("JSON 根元素之后存在多余字符，位置 " + p.pos);
        }
        return v;
    }

    public static Value parse(Reader reader) {
        return parse(readFully(reader));
    }

    private static String readFully(Reader reader) {
        try (reader) {
            StringBuilder sb = new StringBuilder();
            char[] buf = new char[4096];
            int n;
            while ((n = reader.read(buf)) != -1) {
                sb.append(buf, 0, n);
                if (sb.length() > 16 * 1024 * 1024) {
                    throw new JsonException("请求体超过 16 MiB 上限");
                }
            }
            return sb.toString();
        } catch (IOException e) {
            throw new JsonException("读取请求体失败", e);
        }
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }

        char peek() {
            if (pos >= s.length()) {
                throw new JsonException("意外的输入结束，位置 " + pos);
            }
            return s.charAt(pos);
        }

        char next() {
            if (pos >= s.length()) {
                throw new JsonException("意外的输入结束，位置 " + pos);
            }
            return s.charAt(pos++);
        }

        void expect(char c) {
            char a = next();
            if (a != c) {
                throw new JsonException("期望 '" + c + "' 但遇到 '" + a + "'，位置 " + (pos - 1));
            }
        }

        void skipWhitespace() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') {
                    pos++;
                } else {
                    break;
                }
            }
        }

        Value readValue() {
            skipWhitespace();
            if (eof()) throw new JsonException("空输入，无法解析 JSON");
            return switch (peek()) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> new Str(readString());
                case 't', 'f' -> readBoolean();
                case 'n' -> readNull();
                default -> readNumber();
            };
        }

        Obj readObject() {
            expect('{');
            Obj obj = new Obj();
            skipWhitespace();
            if (peek() == '}') { next(); return obj; }
            while (true) {
                skipWhitespace();
                String key = readString();
                skipWhitespace();
                expect(':');
                Value value = readValue();
                obj.put(key, value);
                skipWhitespace();
                char c = next();
                if (c == ',') continue;
                if (c == '}') break;
                throw new JsonException("对象中期望 ',' 或 '}'，遇到 '" + c + "'，位置 " + (pos - 1));
            }
            return obj;
        }

        Arr readArray() {
            expect('[');
            Arr arr = new Arr();
            skipWhitespace();
            if (peek() == ']') { next(); return arr; }
            while (true) {
                Value value = readValue();
                arr.add(value);
                skipWhitespace();
                char c = next();
                if (c == ',') continue;
                if (c == ']') break;
                throw new JsonException("数组中期望 ',' 或 ']'，遇到 '" + c + "'，位置 " + (pos - 1));
            }
            return arr;
        }

        Bool readBoolean() {
            if (s.startsWith("true", pos)) { pos += 4; return Bool.TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return Bool.FALSE; }
            throw new JsonException("非法的布尔字面量，位置 " + pos);
        }

        Value readNull() {
            if (s.startsWith("null", pos)) { pos += 4; return Nul.INSTANCE; }
            throw new JsonException("非法的 null 字面量，位置 " + pos);
        }

        Value readNumber() {
            int start = pos;
            if (peek() == '-') next();
            if (eof() || !Character.isDigit(peek())) {
                throw new JsonException("非法的数字字面量，位置 " + pos);
            }
            while (!eof() && Character.isDigit(peek())) next();
            if (!eof() && peek() == '.') {
                next();
                if (eof() || !Character.isDigit(peek())) {
                    throw new JsonException("非法的小数部分，位置 " + pos);
                }
                while (!eof() && Character.isDigit(peek())) next();
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                next();
                if (!eof() && (peek() == '+' || peek() == '-')) next();
                if (eof() || !Character.isDigit(peek())) {
                    throw new JsonException("非法的指数部分，位置 " + pos);
                }
                while (!eof() && Character.isDigit(peek())) next();
            }
            String token = s.substring(start, pos);
            try {
                return new Num(new BigDecimal(token));
            } catch (NumberFormatException e) {
                throw new JsonException("无法解析数字: " + token, e);
            }
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                char c = next();
                if (c == '"') {
                    return sb.toString();
                }
                if (c < 0x20) {
                    throw new JsonException("字符串中存在未转义的控制字符，位置 " + (pos - 1));
                }
                if (c == '\\') {
                    char e = next();
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
                            int cp = readHex4();
                            if (Character.isHighSurrogate((char) cp)) {
                                if (pos + 5 < s.length() && s.charAt(pos) == '\\' && s.charAt(pos + 1) == 'u') {
                                    pos += 2;
                                    int lo = readHex4();
                                    if (Character.isLowSurrogate((char) lo)) {
                                        sb.appendCodePoint(Character.toCodePoint((char) cp, (char) lo));
                                    } else {
                                        sb.append((char) cp);
                                        sb.append((char) lo);
                                    }
                                } else {
                                    sb.append((char) cp);
                                }
                            } else {
                                sb.append((char) cp);
                            }
                        }
                        default -> throw new JsonException("非法转义字符 \\" + e + "，位置 " + (pos - 1));
                    }
                } else {
                    sb.append(c);
                }
            }
        }

        int readHex4() {
            if (pos + 4 > s.length()) {
                throw new JsonException("\\u 转义不完整，位置 " + pos);
            }
            String hex = s.substring(pos, pos + 4);
            pos += 4;
            try {
                return Integer.parseInt(hex, 16);
            } catch (NumberFormatException ex) {
                throw new JsonException("非法的 \\u 十六进制: " + hex, ex);
            }
        }
    }

    // ---------------------------------------------------------------------
    // 序列化（带缩进，便于人读）
    // ---------------------------------------------------------------------

    public static String write(Value v) {
        StringBuilder sb = new StringBuilder();
        write(v, sb, 0);
        sb.append('\n');
        return sb.toString();
    }

    private static void write(Value v, StringBuilder sb, int indent) {
        switch (v) {
            case Obj obj -> {
                if (obj.isEmpty()) { sb.append("{}"); return; }
                sb.append("{\n");
                boolean first = true;
                for (Map.Entry<String, Value> e : obj.entrySet()) {
                    if (!first) sb.append(",\n");
                    first = false;
                    indent(sb, indent + 1);
                    writeString(sb, e.getKey());
                    sb.append(": ");
                    write(e.getValue(), sb, indent + 1);
                }
                sb.append('\n');
                indent(sb, indent);
                sb.append('}');
            }
            case Arr arr -> {
                if (arr.isEmpty()) { sb.append("[]"); return; }
                sb.append("[\n");
                for (int i = 0; i < arr.size(); i++) {
                    if (i > 0) sb.append(",\n");
                    indent(sb, indent + 1);
                    write(arr.get(i), sb, indent + 1);
                }
                sb.append('\n');
                indent(sb, indent);
                sb.append(']');
            }
            case Str s -> writeString(sb, s.value());
            case Num n -> sb.append(n.value().toPlainString());
            case Bool b -> sb.append(b.value());
            case Nul ignored -> sb.append("null");
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
                case '"' -> sb.append("\\\"");
                case '\\' -> sb.append("\\\\");
                case '\b' -> sb.append("\\b");
                case '\f' -> sb.append("\\f");
                case '\n' -> sb.append("\\n");
                case '\r' -> sb.append("\\r");
                case '\t' -> sb.append("\\t");
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

    // ---------------------------------------------------------------------
    // 规范化序列化与载荷哈希
    // ---------------------------------------------------------------------

    /**
     * 规范化 JSON：无空白；对象键按字典序；数字去除尾零（0 统一为 "0"）。
     * 用于“同 ID 不同载荷”的载荷指纹比较。
     */
    public static String canonical(Value v) {
        StringBuilder sb = new StringBuilder();
        writeCanonical(v, sb);
        return sb.toString();
    }

    private static void writeCanonical(Value v, StringBuilder sb) {
        switch (v) {
            case Obj obj -> {
                sb.append('{');
                // 按键排序后输出，保证语义相等的对象规范化结果一致
                var sorted = new ArrayList<>(obj.entrySet());
                sorted.sort(Map.Entry.comparingByKey());
                for (int i = 0; i < sorted.size(); i++) {
                    if (i > 0) sb.append(',');
                    writeString(sb, sorted.get(i).getKey());
                    sb.append(':');
                    writeCanonical(sorted.get(i).getValue(), sb);
                }
                sb.append('}');
            }
            case Arr arr -> {
                sb.append('[');
                for (int i = 0; i < arr.size(); i++) {
                    if (i > 0) sb.append(',');
                    writeCanonical(arr.get(i), sb);
                }
                sb.append(']');
            }
            case Str s -> writeString(sb, s.value());
            case Num n -> sb.append(canonicalNumber(n.value()));
            case Bool b -> sb.append(b.value());
            case Nul ignored -> sb.append("null");
        }
    }

    private static String canonicalNumber(BigDecimal b) {
        if (b.signum() == 0) return "0";
        return b.stripTrailingZeros().toPlainString();
    }

    /** 载荷的 SHA-256 指纹（基于规范化 JSON）。载荷缺失（null）也有稳定指纹。 */
    public static String sha256Canonical(Value v) {
        byte[] bytes = canonical(v).getBytes(StandardCharsets.UTF_8);
        try {
            MessageDigest md = MessageDigest.getInstance("SHA-256");
            byte[] digest = md.digest(bytes);
            StringBuilder sb = new StringBuilder(64);
            for (byte b : digest) {
                sb.append(String.format("%02x", b));
            }
            return sb.toString();
        } catch (NoSuchAlgorithmException e) {
            throw new IllegalStateException("当前 JDK 不支持 SHA-256", e);
        }
    }

    // ---------------------------------------------------------------------
    // 小工具
    // ---------------------------------------------------------------------

    /** 要求 value 是字符串并返回其内容。 */
    public static String requireStr(Value v, String field) {
        if (v instanceof Str s) {
            if (s.value().isEmpty()) {
                throw new JsonException("字段 " + field + " 不允许为空字符串");
            }
            return s.value();
        }
        throw new JsonException("字段 " + field + " 必须是字符串");
    }

    /** 把 JSON 数字精确转为 long（不允许小数/越界）。 */
    public static long longExact(Value v, String field) {
        if (v instanceof Num n) {
            try {
                return n.value().longValueExact();
            } catch (ArithmeticException e) {
                throw new JsonException("字段 " + field + " 必须是整数毫秒时间戳");
            }
        }
        throw new JsonException("字段 " + field + " 必须是整数毫秒时间戳");
    }
}
