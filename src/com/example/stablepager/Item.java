package com.example.stablepager;

import java.util.LinkedHashMap;
import java.util.Map;

/**
 * An immutable data row. {@code id} is globally unique and is appended after the
 * (possibly duplicate) sort key as the final tie-breaker, which makes the page
 * order total and stable.
 */
public record Item(String id, String name, String category, long score, long createdAt, long updatedAt) {

    public Item withPatch(String name, String category, Long score, long nowMillis) {
        return new Item(
                this.id,
                name != null ? name : this.name,
                category != null ? category : this.category,
                score != null ? score : this.score,
                this.createdAt,
                nowMillis);
    }

    public Map<String, Object> toJson() {
        Map<String, Object> m = new LinkedHashMap<>();
        m.put("id", id);
        m.put("name", name);
        m.put("category", category);
        m.put("score", score);
        m.put("createdAt", createdAt);
        m.put("updatedAt", updatedAt);
        return m;
    }
}
