package com.example.stablepager;

import java.util.Arrays;
import java.util.List;

/**
 * A parsed list query: filters, sort tuple and page size. The {@code fingerprint}
 * is the canonical description of everything that affects the shape/order of the
 * result set; a cursor is only valid when presented together with the exact same
 * fingerprint, so changing any filter/sort/order/limit makes the old cursor
 * unusable.
 */
public record QuerySpec(String nameContains, String category, String sortField, boolean ascending, int limit) {

    public static final int DEFAULT_LIMIT = 20;
    public static final int MAX_LIMIT = 100;
    public static final List<String> SORTABLE_FIELDS = Arrays.asList("score", "name", "createdAt", "updatedAt");

    public static QuerySpec parse(String nameContains, String category, String sortField, String order,
                                  String limitParam) {
        String sort = sortField == null || sortField.isBlank() ? "score" : sortField;
        if (!SORTABLE_FIELDS.contains(sort)) {
            throw ApiException.badRequest("INVALID_SORT",
                    "sort must be one of " + SORTABLE_FIELDS + " but was '" + sort + "'");
        }
        String ord = order == null || order.isBlank() ? "asc" : order.toLowerCase();
        boolean ascending;
        if ("asc".equals(ord)) {
            ascending = true;
        } else if ("desc".equals(ord)) {
            ascending = false;
        } else {
            throw ApiException.badRequest("INVALID_ORDER", "order must be 'asc' or 'desc' but was '" + order + "'");
        }
        int limit = DEFAULT_LIMIT;
        if (limitParam != null && !limitParam.isBlank()) {
            try {
                limit = Integer.parseInt(limitParam);
            } catch (NumberFormatException e) {
                throw ApiException.badRequest("INVALID_LIMIT", "limit must be an integer but was '" + limitParam + "'");
            }
            if (limit < 1 || limit > MAX_LIMIT) {
                throw ApiException.badRequest("INVALID_LIMIT",
                        "limit must be between 1 and " + MAX_LIMIT + " but was " + limit);
            }
        }
        return new QuerySpec(
                emptyToNull(nameContains),
                emptyToNull(category),
                sort,
                ascending,
                limit);
    }

    private static String emptyToNull(String s) {
        return s == null || s.isBlank() ? null : s;
    }

    /** Exact, order-independent description of the query embedded into the cursor. */
    public String fingerprint() {
        return "name=" + nz(nameContains)
                + "|category=" + nz(category)
                + "|sort=" + sortField
                + "|order=" + (ascending ? "asc" : "desc")
                + "|limit=" + limit;
    }

    private static String nz(String s) {
        return s == null ? "" : s;
    }

    public boolean matches(Item item) {
        if (nameContains != null && !item.name().toLowerCase().contains(nameContains.toLowerCase())) {
            return false;
        }
        if (category != null && !category.equals(item.category())) {
            return false;
        }
        return true;
    }

    /** The user-visible sort value (the tie-breaker {@code id} is handled separately). */
    public Comparable<?> sortValue(Item item) {
        return switch (sortField) {
            case "score" -> item.score();
            case "name" -> item.name();
            case "createdAt" -> item.createdAt();
            case "updatedAt" -> item.updatedAt();
            default -> throw new IllegalStateException("unexpected sort field " + sortField);
        };
    }
}
