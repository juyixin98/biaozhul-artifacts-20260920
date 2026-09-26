package com.example.intervals.error;

/**
 * Unchecked exception carrying an {@link ErrorCode} and a human-readable
 * message. Thrown at system boundaries (JSON parsing, validation, expression
 * evaluation) and translated into the JSON error envelope by the CLI.
 */
public final class IntervalException extends RuntimeException {

    private static final long serialVersionUID = 1L;

    private final ErrorCode code;

    public IntervalException(ErrorCode code, String message) {
        super(message);
        this.code = code;
    }

    public IntervalException(ErrorCode code, String message, Throwable cause) {
        super(message, cause);
        this.code = code;
    }

    public ErrorCode errorCode() {
        return code;
    }

    public static IntervalException invalidEndpoint(String message) {
        return new IntervalException(ErrorCode.INVALID_ENDPOINT, message);
    }

    public static IntervalException reversed(String message) {
        return new IntervalException(ErrorCode.REVERSED_INTERVAL, message);
    }
}
