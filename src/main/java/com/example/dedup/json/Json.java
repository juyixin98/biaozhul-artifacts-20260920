package com.example.dedup.json;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal, dependency-free JSON parser / writer / canonicalizer.
 *
 * Supports objects (preserving insertion order), arrays, strings, numbers
 * (parsed as long when integral, double otherwise), booleans and null.
 * The type hierarchy is sealed and exposed as nested records/interfaces.
 */
public final class Json {

    private Json() {
    }

    /** JSON value union type. */
    public sealed interface Value permits JsonObject, JsonArray, JsonString, JsonNumber, JsonBool, JsonNull {
    }

    public record JsonObject(Map<String, Value> members) implements Value {
        public JsonObject {
            members = (members == null) ? new LinkedHashMap<>() : members;
        }

        public JsonObject() {
            this(new LinkedHashMap<>());
        }

        public Value get(String key) {
            return members.get(key);
        }

        public String getString(String key, String dflt) {
            Value v = members.get(key);
            return (v instanceof JsonString s) ? s.value() : dflt;
        }

        public long getLong(String key, long dflt) {
            Value v = members.get(key);
            if (v instanceof JsonNumber n) {
                return n.isIntegral() ? n.longValue() : (long) n.doubleValue();
            }
            return dflt;
        }

        public boolean has(String key) {
            return members.containsKey(key);
        }
    }

    public record JsonArray(List<Value> elements) implements Value {
        public JsonArray {
            elements = (elements == null) ? new java.util.ArrayList<>() : elements;
        }

        public JsonArray() {
            this(new java.util.ArrayList<>());
        }
    }

    public record JsonString(String value) implements Value {
    }

    public record JsonNumber(double value, boolean integral) implements Value {
        public long longValue() {
            return (long) value;
        }

        public double doubleValue() {
            return value;
        }

        public boolean isIntegral() {
            return integral;
        }
    }

    public record JsonBool(boolean value) implements Value {
        public static final JsonBool TRUE = new JsonBool(true);
        public static final JsonBool FALSE = new JsonBool(false);

        public static JsonBool of(boolean b) {
            return b ? TRUE : FALSE;
        }
    }

    public enum JsonNull implements Value {
        INSTANCE
    }

    // ----------------------------------------------------------------
    // Parsing
    // ----------------------------------------------------------------

    public static Value parse(String text) {
        Parser p = new Parser(text);
        p.skipWs();
        Value v = p.readValue();
        p.skipWs();
        if (!p.eof()) {
            throw new JsonException("Trailing characters at position " + p.pos);
        }
        return v;
    }

    public static JsonObject parseObject(String text) {
        Value v = parse(text);
        if (!(v instanceof JsonObject o)) {
            throw new JsonException("Expected JSON object at top level");
        }
        return o;
    }

    public static final class JsonException extends RuntimeException {
        private static final long serialVersionUID = 1L;

        public JsonException(String msg) {
            super(msg);
        }
    }

    private static final class Parser {
        final String s;
        int pos;

        Parser(String s) {
            this.s = s;
        }

        boolean eof() {
            return pos >= s.length();
        }

        char peek() {
            return s.charAt(pos);
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

        Value readValue() {
            if (eof()) {
                throw new JsonException("Unexpected end of input");
            }
            char c = peek();
            return switch (c) {
                case '{' -> readObject();
                case '[' -> readArray();
                case '"' -> new JsonString(readString());
                case 't', 'f' -> readBool();
                case 'n' -> readNull();
                default -> readNumber();
            };
        }

        JsonObject readObject() {
            expect('{');
            JsonObject obj = new JsonObject();
            skipWs();
            if (!eof() && peek() == '}') {
                pos++;
                return obj;
            }
            while (true) {
                skipWs();
                String key = readString();
                skipWs();
                expect(':');
                skipWs();
                Value val = readValue();
                obj.members.put(key, val);
                skipWs();
                if (eof()) {
                    throw new JsonException("Unterminated object");
                }
                char c = s.charAt(pos++);
                if (c == '}') {
                    break;
                }
                if (c != ',') {
                    throw new JsonException("Expected ',' or '}' at position " + (pos - 1));
                }
            }
            return obj;
        }

        JsonArray readArray() {
            expect('[');
            JsonArray arr = new JsonArray();
            skipWs();
            if (!eof() && peek() == ']') {
                pos++;
                return arr;
            }
            while (true) {
                skipWs();
                arr.elements.add(readValue());
                skipWs();
                if (eof()) {
                    throw new JsonException("Unterminated array");
                }
                char c = s.charAt(pos++);
                if (c == ']') {
                    break;
                }
                if (c != ',') {
                    throw new JsonException("Expected ',' or ']' at position " + (pos - 1));
                }
            }
            return arr;
        }

        String readString() {
            expect('"');
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) {
                    throw new JsonException("Unterminated string");
                }
                char c = s.charAt(pos++);
                if (c == '"') {
                    break;
                }
                if (c == '\\') {
                    if (eof()) {
                        throw new JsonException("Unterminated escape");
                    }
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
                            if (pos + 4 > s.length()) {
                                throw new JsonException("Bad unicode escape");
                            }
                            int cp = Integer.parseInt(s.substring(pos, pos + 4), 16);
                            sb.append((char) cp);
                            pos += 4;
                        }
                        default -> throw new JsonException("Bad escape \\" + e);
                    }
                } else {
                    sb.append(c);
                }
            }
            return sb.toString();
        }

        Value readBool() {
            if (s.startsWith("true", pos)) {
                pos += 4;
                return JsonBool.TRUE;
            }
            if (s.startsWith("false", pos)) {
                pos += 5;
                return JsonBool.FALSE;
            }
            throw new JsonException("Invalid literal at position " + pos);
        }

        Value readNull() {
            if (s.startsWith("null", pos)) {
                pos += 4;
                return JsonNull.INSTANCE;
            }
            throw new JsonException("Invalid literal at position " + pos);
        }

        Value readNumber() {
            int start = pos;
            if (!eof() && peek() == '-') {
                pos++;
            }
            boolean anyDigit = false;
            boolean leadingZero = false;
            if (!eof() && Character.isDigit(peek())) {
                leadingZero = peek() == '0';
                anyDigit = true;
                pos++;
            }
            while (!eof() && Character.isDigit(peek())) {
                if (leadingZero) {
                    throw new JsonException("Leading zeros are not allowed at position " + start);
                }
                pos++;
            }
            boolean integral = true;
            if (!eof() && peek() == '.') {
                integral = false;
                leadingZero = false; // fraction may begin with 0
                pos++;
                int frac = 0;
                while (!eof() && Character.isDigit(peek())) {
                    pos++;
                    frac++;
                }
                if (frac == 0) {
                    throw new JsonException("Expected fraction digits at position " + pos);
                }
            }
            if (!eof() && (peek() == 'e' || peek() == 'E')) {
                integral = false;
                pos++;
                if (!eof() && (peek() == '+' || peek() == '-')) {
                    pos++;
                }
                while (!eof() && Character.isDigit(peek())) {
                    pos++;
                }
            }
            if (!anyDigit) {
                throw new JsonException("Invalid number at position " + start);
            }
            String token = s.substring(start, pos);
            if (integral) {
                try {
                    return new JsonNumber(Long.parseLong(token), true);
                } catch (NumberFormatException ex) {
                    return new JsonNumber(Double.parseDouble(token), false);
                }
            }
            return new JsonNumber(Double.parseDouble(token), false);
        }

        void expect(char c) {
            if (eof() || s.charAt(pos) != c) {
                throw new JsonException("Expected '" + c + "' at position " + pos);
            }
            pos++;
        }
    }

    // ----------------------------------------------------------------
    // Writing
    // ----------------------------------------------------------------

    public static String write(Value v) {
        StringBuilder sb = new StringBuilder();
        writeTo(sb, v);
        return sb.toString();
    }

    public static void writeTo(StringBuilder sb, Value v) {
        switch (v) {
            case JsonObject o -> {
                sb.append('{');
                boolean first = true;
                for (Map.Entry<String, Value> e : o.members.entrySet()) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeString(sb, e.getKey());
                    sb.append(':');
                    writeTo(sb, e.getValue());
                }
                sb.append('}');
            }
            case JsonArray a -> {
                sb.append('[');
                boolean first = true;
                for (Value el : a.elements) {
                    if (!first) {
                        sb.append(',');
                    }
                    first = false;
                    writeTo(sb, el);
                }
                sb.append(']');
            }
            case JsonString s -> writeString(sb, s.value());
            case JsonNumber n -> {
                if (n.integral) {
                    sb.append(n.longValue());
                } else {
                    sb.append(n.doubleValue());
                }
            }
            case JsonBool b -> sb.append(b.value());
            case JsonNull ignored -> sb.append("null");
        }
    }

    static void writeString(StringBuilder sb, String s) {
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

    // ----------------------------------------------------------------
    // Convenience constructors
    // ----------------------------------------------------------------

    public static JsonString str(String s) {
        return new JsonString(s == null ? "" : s);
    }

    public static Value num(long n) {
        return new JsonNumber(n, true);
    }

    public static Value num(double d) {
        return new JsonNumber(d, false);
    }

    public static JsonObject obj() {
        return new JsonObject();
    }

    public static JsonArray arr() {
        return new JsonArray();
    }

    /** Deterministic canonical form: object keys sorted lexicographically. */
    public static String canonical(Value v) {
        StringBuilder sb = new StringBuilder();
        canonicalTo(sb, v);
        return sb.toString();
    }

    private static void canonicalTo(StringBuilder sb, Value v) {
        switch (v) {
            case JsonObject o -> {
                sb.append('{');
                List<String> keys = new java.util.ArrayList<>(o.members.keySet());
                Collections.sort(keys);
                for (int i = 0; i < keys.size(); i++) {
                    if (i > 0) {
                        sb.append(',');
                    }
                    writeString(sb, keys.get(i));
                    sb.append(':');
                    canonicalTo(sb, o.members.get(keys.get(i)));
                }
                sb.append('}');
            }
            default -> writeTo(sb, v);
        }
    }
}
