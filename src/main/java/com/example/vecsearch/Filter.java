package com.example.vecsearch;

import java.util.Map;

/**
 * Metadata filter: all conditions must match (logical AND of equality
 * predicates), e.g. {"category": "electronics", "active": true}.
 *
 * Equality follows JSON semantics: strings and booleans match exactly,
 * numbers compare by numeric value (1 == 1.0), null matches an explicit
 * JSON null. A missing key never matches.
 */
public final class Filter {

    private final Map<String, Object> conditions;

    private Filter(Map<String, Object> conditions) {
        this.conditions = conditions;
    }

    /** Empty filter means "match everything". */
    public static Filter from(Map<String, Object> conditions) {
        return new Filter(conditions);
    }

    public boolean isEmpty() {
        return conditions == null || conditions.isEmpty();
    }

    public boolean matches(Map<String, Object> metadata) {
        if (isEmpty()) {
            return true;
        }
        if (metadata == null) {
            return false;
        }
        for (Map.Entry<String, Object> e : conditions.entrySet()) {
            if (!metadata.containsKey(e.getKey())) {
                return false;
            }
            if (!jsonEquals(metadata.get(e.getKey()), e.getValue())) {
                return false;
            }
        }
        return true;
    }

    private static boolean jsonEquals(Object a, Object b) {
        if (a == null || b == null) {
            return a == null && b == null;
        }
        if (a instanceof Number && b instanceof Number) {
            return Double.compare(((Number) a).doubleValue(), ((Number) b).doubleValue()) == 0;
        }
        // Numbers never equal strings/booleans even if toString collides.
        if (a instanceof Number || b instanceof Number) {
            return false;
        }
        return a.equals(b);
    }
}
