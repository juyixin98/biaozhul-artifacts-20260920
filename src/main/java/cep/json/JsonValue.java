package cep.json;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;

/**
 * 迷你 JSON 值模型（零外部依赖）。只提供本项目需要的类型与取值辅助方法。
 */
public sealed interface JsonValue permits JsonValue.Obj, JsonValue.Arr,
        JsonValue.Str, JsonValue.Num, JsonValue.Bool, JsonValue.Null {

    static Obj obj() { return new Obj(); }

    static Arr arr() { return new Arr(); }

    static JsonValue of(String s) { return s == null ? Null.INSTANCE : new Str(s); }

    static JsonValue of(long n) { return new Num(n); }

    static JsonValue of(double n) { return new Num(n); }

    static JsonValue of(boolean b) { return b ? Bool.TRUE : Bool.FALSE; }

    static JsonValue nul() { return Null.INSTANCE; }

    default Obj asObj() {
        throw new JsonException("期望 object，实际是 " + typeName());
    }

    default Arr asArr() {
        throw new JsonException("期望 array，实际是 " + typeName());
    }

    default String asString() {
        throw new JsonException("期望 string，实际是 " + typeName());
    }

    default long asLong() {
        throw new JsonException("期望 number，实际是 " + typeName());
    }

    default double asDouble() {
        throw new JsonException("期望 number，实际是 " + typeName());
    }

    default boolean asBool() {
        throw new JsonException("期望 boolean，实际是 " + typeName());
    }

    default String typeName() {
        return getClass().getSimpleName().toLowerCase();
    }

    final class Obj implements JsonValue {
        private final LinkedHashMap<String, JsonValue> map = new LinkedHashMap<>();

        public Obj set(String key, JsonValue value) {
            map.put(key, value == null ? Null.INSTANCE : value);
            return this;
        }

        public Obj set(String key, String value) { return set(key, JsonValue.of(value)); }
        public Obj set(String key, long value) { return set(key, JsonValue.of(value)); }
        public Obj set(String key, boolean value) { return set(key, JsonValue.of(value)); }

        public JsonValue get(String key) { return map.get(key); }

        public boolean has(String key) {
            JsonValue v = map.get(key);
            return v != null && !(v instanceof Null);
        }

        public String requireString(String key) { return require(key).asString(); }
        public long requireLong(String key) { return require(key).asLong(); }
        public boolean requireBool(String key) { return require(key).asBool(); }
        public Obj requireObj(String key) { return require(key).asObj(); }
        public Arr requireArr(String key) { return require(key).asArr(); }

        public JsonValue require(String key) {
            JsonValue v = map.get(key);
            if (v == null || v instanceof Null) {
                throw new JsonException("缺少必填字段: " + key);
            }
            return v;
        }

        public String optString(String key, String fallback) {
            return has(key) ? map.get(key).asString() : fallback;
        }

        public long optLong(String key, long fallback) {
            return has(key) ? map.get(key).asLong() : fallback;
        }

        public boolean optBool(String key, boolean fallback) {
            return has(key) ? map.get(key).asBool() : fallback;
        }

        public Set<Map.Entry<String, JsonValue>> entries() { return map.entrySet(); }

        @Override public Obj asObj() { return this; }
    }

    final class Arr implements JsonValue {
        private final List<JsonValue> list = new ArrayList<>();

        public Arr add(JsonValue value) {
            list.add(value == null ? Null.INSTANCE : value);
            return this;
        }

        public Arr add(String value) { return add(JsonValue.of(value)); }
        public Arr add(long value) { return add(JsonValue.of(value)); }

        public int size() { return list.size(); }
        public JsonValue get(int i) { return list.get(i); }
        public List<JsonValue> values() { return list; }

        @Override public Arr asArr() { return this; }
    }

    record Str(String value) implements JsonValue {
        @Override public String asString() { return value; }
    }

    final class Num implements JsonValue {
        private final double value;

        Num(double value) { this.value = value; }

        @Override public long asLong() {
            if (value < Long.MIN_VALUE || value > Long.MAX_VALUE) {
                throw new JsonException("数值超出 long 范围: " + value);
            }
            return (long) value;
        }

        @Override public double asDouble() { return value; }
    }

    final class Bool implements JsonValue {
        static final Bool TRUE = new Bool(true);
        static final Bool FALSE = new Bool(false);
        private final boolean value;

        private Bool(boolean value) { this.value = value; }

        @Override public boolean asBool() { return value; }
    }

    final class Null implements JsonValue {
        static final Null INSTANCE = new Null();
        private Null() {}
    }
}
