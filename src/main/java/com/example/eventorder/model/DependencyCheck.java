package com.example.eventorder.model;

/**
 * Per-dependency verification record, echoed in the response so callers can
 * confirm every requested edge was checked.
 *
 * @param before    dependency source event id
 * @param after     dependency target event id
 * @param satisfied true when the emitted order places {@code before} ahead of {@code after};
 *                  false when the request is unsatisfiable (no order could honour it)
 */
public record DependencyCheck(String before, String after, boolean satisfied) {
}
