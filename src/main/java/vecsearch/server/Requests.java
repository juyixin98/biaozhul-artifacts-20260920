package vecsearch.server;

import vecsearch.core.TaggedVector;
import vecsearch.util.ApiException;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** 把解析后的 JSON（Map/List/Double）安全转换为领域对象，类型错误一律 400。 */
final class Requests {

    private Requests() {
    }

    static String requireString(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof String s) || s.isEmpty()) {
            throw ApiException.badRequest("'" + key + "' must be a non-empty string");
        }
        return s;
    }

    static String optionalString(Map<String, Object> body, String key, String dflt) {
        Object v = body.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof String s)) {
            throw ApiException.badRequest("'" + key + "' must be a string");
        }
        return s;
    }

    static int requireInt(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof Number n)) {
            throw ApiException.badRequest("'" + key + "' must be a number");
        }
        return Math.toIntExact(n.longValue());
    }

    static int optionalInt(Map<String, Object> body, String key, int dflt) {
        Object v = body.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof Number n)) {
            throw ApiException.badRequest("'" + key + "' must be a number");
        }
        return Math.toIntExact(n.longValue());
    }

    static long optionalLong(Map<String, Object> body, String key, long dflt) {
        Object v = body.get(key);
        if (v == null) {
            return dflt;
        }
        if (!(v instanceof Number n)) {
            throw ApiException.badRequest("'" + key + "' must be a number");
        }
        return n.longValue();
    }

    static float[] requireVector(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (!(v instanceof List<?> list) || list.isEmpty()) {
            throw ApiException.badRequest("'" + key + "' must be a non-empty array of numbers");
        }
        return toFloatVector(list, key);
    }

    static float[] toFloatVector(List<?> list, String key) {
        float[] vec = new float[list.size()];
        for (int i = 0; i < list.size(); i++) {
            Object o = list.get(i);
            if (!(o instanceof Number n)) {
                throw ApiException.badRequest(
                        "'" + key + "'[" + i + "] must be a number");
            }
            vec[i] = n.floatValue();
        }
        return vec;
    }

    @SuppressWarnings("unchecked")
    static Map<String, String> optionalFilter(Map<String, Object> body, String key) {
        Object v = body.get(key);
        if (v == null) {
            return null;
        }
        if (!(v instanceof Map<?, ?> map)) {
            throw ApiException.badRequest("'" + key + "' must be an object of string key-values");
        }
        Map<String, String> out = new LinkedHashMap<>();
        for (Map.Entry<?, ?> e : map.entrySet()) {
            if (!(e.getKey() instanceof String) || e.getValue() == null) {
                throw ApiException.badRequest(
                        "'" + key + "' keys must be strings and values must not be null");
            }
            out.put((String) e.getKey(), String.valueOf(e.getValue()));
        }
        return out;
    }

    /** 解析单条或批量 upsert 请求体。 */
    @SuppressWarnings("unchecked")
    static List<TaggedVector> parseUpsertBody(Map<String, Object> body) {
        // 单条：{"id": ..., "vector": ..., "filter": {...}}
        if (body.containsKey("id") || body.containsKey("vector")) {
            return List.of(parseOne(body));
        }
        Object vectors = body.get("vectors");
        if (!(vectors instanceof List<?> list) || list.isEmpty()) {
            throw ApiException.badRequest(
                    "expected either a single {'id','vector'} object or {'vectors': [...]} batch");
        }
        List<TaggedVector> batch = new ArrayList<>(list.size());
        for (int i = 0; i < list.size(); i++) {
            Object o = list.get(i);
            if (!(o instanceof Map<?, ?> m)) {
                throw ApiException.badRequest("'vectors'[" + i + "] must be an object");
            }
            batch.add(parseOne((Map<String, Object>) m));
        }
        return batch;
    }

    @SuppressWarnings("unchecked")
    private static TaggedVector parseOne(Map<String, Object> body) {
        String id = requireString(body, "id");
        float[] vec = requireVector(body, "vector");
        Map<String, String> filter = optionalFilter(body, "filter");
        return new TaggedVector(id, vec, filter);
    }
}
