package dev.timeprecision.json;

import java.util.List;
import java.util.Map;

/**
 * Minimal JSON value model. Numbers keep their raw literal text so integer
 * timestamps are never routed through floating point.
 */
public sealed interface JsonValue {

    record Obj(Map<String, JsonValue> members) implements JsonValue {
        public JsonValue get(String key) {
            return members.get(key);
        }

        public boolean has(String key) {
            return members.containsKey(key);
        }
    }

    record Arr(List<JsonValue> items) implements JsonValue {
    }

    record Str(String value) implements JsonValue {
    }

    /** A JSON number preserved as its source literal (never parsed to double). */
    record Num(String raw) implements JsonValue {
    }

    record Bool(boolean value) implements JsonValue {
    }

    enum Null implements JsonValue {
        INSTANCE
    }
}
