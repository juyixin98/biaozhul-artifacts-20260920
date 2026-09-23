package com.opp16.engine.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/**
 * Minimal self-contained JSON parser / writer.
 *
 * Supports objects, arrays, strings, numbers (long or double), booleans and null.
 * No external dependencies are used anywhere in the project.
 */
public final class Json {

    private Json() {}

    /** Raised on malformed JSON or wrong-typed value access. */
    public static class JsonException extends RuntimeException {
        private static final long serialVersionUID = 1L;
        public JsonException(String message) { super(message); }
    }

    // ----------------------------------------------------------------------
    // Value model
    // ----------------------------------------------------------------------

    public static abstract class Value {
        public boolean isObject()   { return false; }
        public boolean isArray()    { return false; }
        public boolean isString()   { return false; }
        public boolean isNumber()   { return false; }
        public boolean isBoolean()  { return false; }
        public boolean isNull()     { return false; }

        public Obj asObject() { throw new JsonException("expected object, got " + typeName()); }
        public Arr asArray()  { throw new JsonException("expected array, got " + typeName()); }
        public String asString() { throw new JsonException("expected string, got " + typeName()); }
        public long asLong() { throw new JsonException("expected integer, got " + typeName()); }
        public double asDouble() {
            if (isNumber()) return ((Num) this).doubleValue;
            throw new JsonException("expected number, got " + typeName());
        }
        public boolean asBoolean() { throw new JsonException("expected boolean, got " + typeName()); }

        abstract String typeName();

        /** Structured equality (used by the test harness). */
        @Override public abstract boolean equals(Object other);
        @Override public abstract int hashCode();

        public String render() { return render(false); }
        public String render(boolean pretty) {
            StringBuilder sb = new StringBuilder();
            write(sb, this, pretty, 0);
            return sb.toString();
        }

        @Override public String toString() { return render(); }
    }

    public static final class Obj extends Value {
        public final LinkedHashMap<String, Value> map = new LinkedHashMap<>();

        @Override public boolean isObject() { return true; }
        @Override public Obj asObject() { return this; }
        @Override String typeName() { return "object"; }

        public Obj put(String key, Value v) { map.put(key, v == null ? NULL : v); return this; }
        public Obj put(String key, String v) { return put(key, v == null ? NULL : new Str(v)); }
        public Obj put(String key, long v) { return put(key, new Num(v)); }
        public Obj put(String key, double v) { return put(key, new Num(v)); }
        public Obj put(String key, boolean v) { return put(key, v ? TRUE : FALSE); }

        public boolean has(String key) { return map.containsKey(key); }
        public Value get(String key) {
            Value v = map.get(key);
            if (v == null) throw new JsonException("missing key: " + key);
            return v;
        }
        public Value getOr(String key, Value fallback) {
            Value v = map.get(key);
            return v == null ? fallback : v;
        }
        public String getString(String key) {
            Value v = get(key);
            return v.isNull() ? null : v.asString();
        }
        public Long getLongBoxed(String key) {
            Value v = get(key);
            return v.isNull() ? null : v.asLong();
        }
        public int getInt(String key, int def) {
            Value v = map.get(key);
            return v == null || v.isNull() ? def : (int) v.asLong();
        }
        public boolean getBool(String key, boolean def) {
            Value v = map.get(key);
            return v == null || v.isNull() ? def : v.asBoolean();
        }

        @Override public boolean equals(Object o) {
            return o instanceof Obj && ((Obj) o).map.equals(map);
        }
        @Override public int hashCode() { return map.hashCode(); }
    }

    public static final class Arr extends Value {
        public final List<Value> list = new ArrayList<>();

        @Override public boolean isArray() { return true; }
        @Override public Arr asArray() { return this; }
        @Override String typeName() { return "array"; }

        public Arr add(Value v) { list.add(v == null ? NULL : v); return this; }
        public Arr add(long v) { list.add(new Num(v)); return this; }
        public Arr add(String v) { list.add(v == null ? NULL : new Str(v)); return this; }
        public int size() { return list.size(); }
        public Value get(int i) { return list.get(i); }

        @Override public boolean equals(Object o) {
            return o instanceof Arr && ((Arr) o).list.equals(list);
        }
        @Override public int hashCode() { return list.hashCode(); }
    }

    public static final class Str extends Value {
        public final String value;
        public Str(String value) { this.value = value; }
        @Override public boolean isString() { return true; }
        @Override public String asString() { return value; }
        @Override String typeName() { return "string"; }
        @Override public boolean equals(Object o) { return o instanceof Str && ((Str) o).value.equals(value); }
        @Override public int hashCode() { return value.hashCode(); }
    }

    public static final class Num extends Value {
        public final boolean decimal;
        public final long longValue;
        public final double doubleValue;

        public Num(long v) { decimal = false; longValue = v; doubleValue = v; }
        public Num(double v) { decimal = true; longValue = (long) v; doubleValue = v; }

        @Override public boolean isNumber() { return true; }
        @Override public long asLong() {
            if (decimal) return longValue;
            return longValue;
        }
        @Override public double asDouble() { return doubleValue; }
        @Override String typeName() { return "number"; }

        @Override public boolean equals(Object o) {
            if (!(o instanceof Num)) return false;
            Num n = (Num) o;
            if (decimal != n.decimal) return false;
            return decimal ? Double.compare(doubleValue, n.doubleValue) == 0
                           : longValue == n.longValue;
        }
        @Override public int hashCode() { return decimal ? Double.hashCode(doubleValue) : Long.hashCode(longValue); }
    }

    public static final class Bool extends Value {
        public final boolean value;
        public Bool(boolean value) { this.value = value; }
        @Override public boolean isBoolean() { return true; }
        @Override public boolean asBoolean() { return value; }
        @Override String typeName() { return "boolean"; }
        @Override public boolean equals(Object o) { return o instanceof Bool && ((Bool) o).value == value; }
        @Override public int hashCode() { return Boolean.hashCode(value); }
    }

    public static final class Null extends Value {
        @Override public boolean isNull() { return true; }
        @Override String typeName() { return "null"; }
        @Override public boolean equals(Object o) { return o instanceof Null; }
        @Override public int hashCode() { return 0; }
    }

    public static final Value NULL = new Null();
    public static final Value TRUE = new Bool(true);
    public static final Value FALSE = new Bool(false);

    public static Value of(String s) { return s == null ? NULL : new Str(s); }

    // ----------------------------------------------------------------------
    // Parsing
    // ----------------------------------------------------------------------

    public static Value parse(String text) {
        Parser p = new Parser(text);
        Value v = p.readValue();
        p.skipWs();
        if (!p.eof()) throw p.error("trailing characters");
        return v;
    }

    private static final class Parser {
        private final String s;
        private int pos;

        Parser(String s) { this.s = s; }

        boolean eof() { return pos >= s.length(); }

        JsonException error(String msg) {
            return new JsonException("JSON error at position " + pos + ": " + msg);
        }

        void skipWs() {
            while (pos < s.length()) {
                char c = s.charAt(pos);
                if (c == ' ' || c == '\t' || c == '\n' || c == '\r') pos++;
                else break;
            }
        }

        char peek() {
            if (eof()) throw error("unexpected end of input");
            return s.charAt(pos);
        }

        Value readValue() {
            skipWs();
            char c = peek();
            switch (c) {
                case '{': return readObject();
                case '[': return readArray();
                case '"': return new Str(readString());
                case 't': case 'f': return readBoolean();
                case 'n': return readNull();
                default:
                    if (c == '-' || (c >= '0' && c <= '9')) return readNumber();
                    throw error("unexpected character '" + c + "'");
            }
        }

        Obj readObject() {
            Obj obj = new Obj();
            pos++; // {
            skipWs();
            if (peek() == '}') { pos++; return obj; }
            while (true) {
                skipWs();
                if (peek() != '"') throw error("expected string key");
                String key = readString();
                skipWs();
                if (peek() != ':') throw error("expected ':'");
                pos++;
                obj.put(key, readValue());
                skipWs();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == '}') { pos++; return obj; }
                throw error("expected ',' or '}'");
            }
        }

        Arr readArray() {
            Arr arr = new Arr();
            pos++; // [
            skipWs();
            if (peek() == ']') { pos++; return arr; }
            while (true) {
                arr.add(readValue());
                skipWs();
                char c = peek();
                if (c == ',') { pos++; continue; }
                if (c == ']') { pos++; return arr; }
                throw error("expected ',' or ']'");
            }
        }

        String readString() {
            pos++; // opening quote
            StringBuilder sb = new StringBuilder();
            while (true) {
                if (eof()) throw error("unterminated string");
                char c = s.charAt(pos++);
                if (c == '"') return sb.toString();
                if (c == '\\') {
                    if (eof()) throw error("unterminated escape");
                    char e = s.charAt(pos++);
                    switch (e) {
                        case '"': sb.append('"'); break;
                        case '\\': sb.append('\\'); break;
                        case '/': sb.append('/'); break;
                        case 'b': sb.append('\b'); break;
                        case 'f': sb.append('\f'); break;
                        case 'n': sb.append('\n'); break;
                        case 'r': sb.append('\r'); break;
                        case 't': sb.append('\t'); break;
                        case 'u':
                            if (pos + 4 > s.length()) throw error("bad unicode escape");
                            sb.append((char) Integer.parseInt(s.substring(pos, pos + 4), 16));
                            pos += 4;
                            break;
                        default: throw error("bad escape \\" + e);
                    }
                } else if (c < 0x20) {
                    throw error("unescaped control character in string");
                } else {
                    sb.append(c);
                }
            }
        }

        Value readBoolean() {
            if (s.startsWith("true", pos)) { pos += 4; return TRUE; }
            if (s.startsWith("false", pos)) { pos += 5; return FALSE; }
            throw error("invalid literal");
        }

        Value readNull() {
            if (s.startsWith("null", pos)) { pos += 4; return NULL; }
            throw error("invalid literal");
        }

        Value readNumber() {
            int start = pos;
            if (peek() == '-') pos++;
            readDigits();
            boolean decimal = false;
            if (!eof() && s.charAt(pos) == '.') {
                decimal = true;
                pos++;
                readDigits();
            }
            if (!eof() && (s.charAt(pos) == 'e' || s.charAt(pos) == 'E')) {
                decimal = true;
                pos++;
                if (!eof() && (s.charAt(pos) == '+' || s.charAt(pos) == '-')) pos++;
                readDigits();
            }
            String token = s.substring(start, pos);
            try {
                return decimal ? new Num(Double.parseDouble(token)) : new Num(Long.parseLong(token));
            } catch (NumberFormatException e) {
                throw error("invalid number " + token);
            }
        }

        void readDigits() {
            int start = pos;
            while (pos < s.length() && Character.isDigit(s.charAt(pos))) pos++;
            if (pos == start) throw error("expected digits");
        }
    }

    // ----------------------------------------------------------------------
    // Writing
    // ----------------------------------------------------------------------

    private static void write(StringBuilder sb, Value v, boolean pretty, int indent) {
        if (v.isNull()) { sb.append("null"); return; }
        if (v.isBoolean()) { sb.append(v.asBoolean()); return; }
        if (v.isString()) { writeString(sb, v.asString()); return; }
        if (v.isNumber()) {
            Num n = (Num) v;
            sb.append(n.decimal ? trimDouble(n.doubleValue) : Long.toString(n.longValue));
            return;
        }
        if (v.isArray()) {
            Arr a = v.asArray();
            if (a.list.isEmpty()) { sb.append("[]"); return; }
            sb.append('[');
            for (int i = 0; i < a.list.size(); i++) {
                if (i > 0) sb.append(',');
                if (pretty) { sb.append('\n'); indent(sb, indent + 1); }
                write(sb, a.list.get(i), pretty, indent + 1);
            }
            if (pretty) { sb.append('\n'); indent(sb, indent); }
            sb.append(']');
            return;
        }
        Obj o = v.asObject();
        if (o.map.isEmpty()) { sb.append("{}"); return; }
        sb.append('{');
        int i = 0;
        for (Map.Entry<String, Value> e : o.map.entrySet()) {
            if (i++ > 0) sb.append(',');
            if (pretty) { sb.append('\n'); indent(sb, indent + 1); }
            writeString(sb, e.getKey());
            sb.append(pretty ? ": " : ":");
            write(sb, e.getValue(), pretty, indent + 1);
        }
        if (pretty) { sb.append('\n'); indent(sb, indent); }
        sb.append('}');
    }

    private static String trimDouble(double d) {
        if (d == Math.floor(d) && !Double.isInfinite(d) && Math.abs(d) < 1e15) {
            return Long.toString((long) d);
        }
        return Double.toString(d);
    }

    private static void indent(StringBuilder sb, int level) {
        for (int i = 0; i < level; i++) sb.append("  ");
    }

    private static void writeString(StringBuilder sb, String s) {
        sb.append('"');
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '"': sb.append("\\\""); break;
                case '\\': sb.append("\\\\"); break;
                case '\b': sb.append("\\b"); break;
                case '\f': sb.append("\\f"); break;
                case '\n': sb.append("\\n"); break;
                case '\r': sb.append("\\r"); break;
                case '\t': sb.append("\\t"); break;
                default:
                    if (c < 0x20) sb.append(String.format("\\u%04x", (int) c));
                    else sb.append(c);
            }
        }
        sb.append('"');
    }
}
