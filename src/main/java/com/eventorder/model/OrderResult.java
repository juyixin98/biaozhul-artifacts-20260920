package com.eventorder.model;

import java.util.List;

/**
 * Result of an ordering computation. Exactly one of the two shapes is populated:
 * OK -> {@code order}; UNSATISFIABLE -> {@code conflicts}; INVALID_INPUT -> {@code errors}.
 */
public record OrderResult(
        String status,
        List<String> order,
        boolean multipleValidOrders,
        List<Conflict> conflicts,
        List<String> errors,
        Diagnostics diagnostics) {

    public record Diagnostics(
            String tzdbVersion,
            int eventCount,
            int edgeCount) {
    }

    public static OrderResult ok(List<String> order, boolean multipleValidOrders, Diagnostics diag) {
        return new OrderResult("OK", List.copyOf(order), multipleValidOrders,
                List.of(), List.of(), diag);
    }

    public static OrderResult unsatisfiable(List<Conflict> conflicts, Diagnostics diag) {
        return new OrderResult("UNSATISFIABLE", List.of(), false,
                List.copyOf(conflicts), List.of(), diag);
    }

    public static OrderResult invalidInput(List<String> errors, String tzdbVersion) {
        return new OrderResult("INVALID_INPUT", List.of(), false,
                List.of(), List.copyOf(errors),
                new Diagnostics(tzdbVersion, 0, 0));
    }
}
