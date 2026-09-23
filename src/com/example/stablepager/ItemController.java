package com.example.stablepager;

import java.util.Map;

/** Maps validated HTTP requests onto the store / pagination service. */
public final class ItemController {

    private final MvccStore store;
    private final PaginationService pagination;

    public ItemController(MvccStore store, PaginationService pagination) {
        this.store = store;
        this.pagination = pagination;
    }

    public Map<String, Object> list(Map<String, String> query) {
        QuerySpec spec = QuerySpec.parse(
                query.get("nameContains"),
                query.get("category"),
                query.get("sort"),
                query.get("order"),
                query.get("limit"));
        String cursor = query.get("cursor");
        return pagination.list(spec, cursor);
    }

    public Map<String, Object> get(String id) {
        return store.getLive(id)
                .orElseThrow(() -> new ApiException(404, "NOT_FOUND", "no item with id '" + id + "'"))
                .toJson();
    }

    public Map<String, Object> create(Map<String, Object> body) {
        String id = requireString(body, "id", true);
        String name = requireString(body, "name", true);
        String category = requireString(body, "category", true);
        long score = requireLong(body, "score", true);
        return store.insert(id, name, category, score).toJson();
    }

    public Map<String, Object> patch(String id, Map<String, Object> body) {
        if (body.isEmpty()) {
            throw ApiException.badRequest("EMPTY_PATCH", "PATCH body must contain at least one of name/category/score");
        }
        String name = body.containsKey("name") ? requireString(body, "name", false) : null;
        String category = body.containsKey("category") ? requireString(body, "category", false) : null;
        Long score = body.containsKey("score") ? requireLong(body, "score", false) : null;
        for (String key : body.keySet()) {
            if (!key.equals("name") && !key.equals("category") && !key.equals("score")) {
                throw ApiException.badRequest("UNKNOWN_FIELD", "field '" + key + "' cannot be patched");
            }
        }
        return store.update(id, name, category, score).toJson();
    }

    public void delete(String id) {
        store.delete(id);
    }

    private static String requireString(Map<String, Object> body, String field, boolean required) {
        Object v = body.get(field);
        if (v == null) {
            if (required) {
                throw ApiException.badRequest("MISSING_FIELD", "field '" + field + "' is required");
            }
            return null;
        }
        if (!(v instanceof String s) || s.isBlank()) {
            throw ApiException.badRequest("INVALID_FIELD", "field '" + field + "' must be a non-empty string");
        }
        return s;
    }

    private static long requireLong(Map<String, Object> body, String field, boolean required) {
        Object v = body.get(field);
        if (v == null) {
            if (required) {
                throw ApiException.badRequest("MISSING_FIELD", "field '" + field + "' is required");
            }
            return 0L;
        }
        if (!(v instanceof Number n)) {
            throw ApiException.badRequest("INVALID_FIELD", "field '" + field + "' must be an integer");
        }
        if (n.doubleValue() != Math.rint(n.doubleValue())) {
            throw ApiException.badRequest("INVALID_FIELD", "field '" + field + "' must be an integer");
        }
        return n.longValue();
    }
}
