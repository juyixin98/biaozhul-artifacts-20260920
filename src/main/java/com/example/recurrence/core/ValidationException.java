package com.example.recurrence.core;

/** Raised when a request or rule fails validation. Carries a stable error code. */
public class ValidationException extends RuntimeException {

    private final String code;

    public ValidationException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
