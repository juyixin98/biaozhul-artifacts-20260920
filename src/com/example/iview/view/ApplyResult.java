package com.example.iview.view;

/**
 * Outcome of applying (or rejecting as a duplicate) one business event.
 *
 * <p>{@code duplicate=true} means the eventId had been seen before: the event
 * was not reapplied. {@code changed=false} on a non-duplicate means the target
 * row already held exactly the submitted state (a no-op upsert) or a delete
 * did not find its row.
 */
public record ApplyResult(
        String eventId,
        boolean duplicate,
        String side,
        String op,
        boolean changed,
        boolean notFound,
        String categoryFrom,
        String categoryTo,
        int migratedOrderLines,
        int unmatchedOrderLines) {
}
