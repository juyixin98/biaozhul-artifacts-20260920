package com.example.dstexpand.engine;

/**
 * Thrown when a request is invalid or a resolution policy of ERROR is hit.
 * Carries a user-facing message; the CLI turns it into a JSON error response.
 */
public class ExpansionException extends RuntimeException {

    public ExpansionException(String message) {
        super(message);
    }
}
