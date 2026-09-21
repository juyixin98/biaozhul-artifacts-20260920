package com.example.itasset.support;

import jakarta.servlet.http.HttpServletResponse;

/**
 * Carries the outcome of idempotency handling for one mutating request.
 * When {@link #replayed()} is true the controller must write the stored response
 * and must not execute the business operation.
 */
public record IdempotencyOutcome(boolean replayed, int storedStatus, String storedBody) {

    public void writeReplay(HttpServletResponse response) {
        if (!replayed) {
            throw new IllegalStateException("not a replay");
        }
        try {
            response.setStatus(storedStatus);
            response.setContentType("application/json;charset=UTF-8");
            response.getWriter().write(storedBody == null ? "" : storedBody);
            response.getWriter().flush();
        } catch (Exception e) {
            throw new IllegalStateException("Failed to write replayed response", e);
        }
    }
}
