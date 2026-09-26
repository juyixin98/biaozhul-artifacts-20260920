package com.example.intervals.error;

/**
 * Base class for all request-rejected conditions (HTTP-ish client errors).
 * Every {@code ErrorCode} carries a stable machine-readable code used in the
 * JSON response envelope.
 */
public enum ErrorCode {
    INVALID_REQUEST("invalid_request"),
    UNKNOWN_DOMAIN("unknown_domain"),
    UNKNOWN_DATASET("unknown_dataset"),
    UNKNOWN_SET_REF("unknown_set_ref"),
    UNKNOWN_OPERATION("unknown_operation"),
    INVALID_ENDPOINT("invalid_endpoint"),
    REVERSED_INTERVAL("reversed_interval"),
    INVALID_EXPRESSION("invalid_expression"),
    ARITY_MISMATCH("arity_mismatch"),
    INTERNAL_ERROR("internal_error");

    private final String code;

    ErrorCode(String code) {
        this.code = code;
    }

    public String code() {
        return code;
    }
}
