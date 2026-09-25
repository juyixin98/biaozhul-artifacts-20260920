package dev.timeprecision.http;

import dev.timeprecision.ConversionException;
import dev.timeprecision.ErrorCode;
import dev.timeprecision.Meta;
import dev.timeprecision.json.JsonValue;

import java.util.LinkedHashMap;
import java.util.Map;

/** Builds JSON response trees. Results are strings so 64-bit values survive JSON exactly. */
final class Responses {

    private Responses() {
    }

    static JsonValue ok(Map<String, JsonValue> payload) {
        Map<String, JsonValue> root = new LinkedHashMap<>();
        root.put("ok", new JsonValue.Bool(true));
        root.putAll(payload);
        root.put("meta", meta());
        return new JsonValue.Obj(root);
    }

    static JsonValue error(ErrorCode code, String message) {
        Map<String, JsonValue> err = new LinkedHashMap<>();
        err.put("code", new JsonValue.Str(code.name()));
        err.put("message", new JsonValue.Str(message));
        Map<String, JsonValue> root = new LinkedHashMap<>();
        root.put("ok", new JsonValue.Bool(false));
        root.put("error", new JsonValue.Obj(err));
        root.put("meta", meta());
        return new JsonValue.Obj(root);
    }

    static JsonValue error(ConversionException e) {
        return error(e.code(), e.getMessage());
    }

    static JsonValue meta() {
        Map<String, JsonValue> meta = new LinkedHashMap<>();
        meta.put("tzdbVersion", new JsonValue.Str(Meta.tzdbVersion()));
        meta.put("zoneCount", new JsonValue.Num(String.valueOf(Meta.zoneCount())));
        meta.put("javaVersion", new JsonValue.Str(Meta.javaVersion()));
        return new JsonValue.Obj(meta);
    }
}
