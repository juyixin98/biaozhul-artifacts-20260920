package com.example.eventorder.engine;

/**
 * Raised when a request fails validation. Carries a caller-readable message;
 * the CLI maps it to exit code 2.
 */
public class RequestException extends RuntimeException {

    public RequestException(String message) {
        super(message);
    }
}
