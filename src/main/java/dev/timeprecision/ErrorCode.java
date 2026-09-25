package dev.timeprecision;

/** Stable machine-readable error codes returned in JSON error responses. */
public enum ErrorCode {
    BAD_REQUEST,
    INVALID_UNIT,
    INVALID_VALUE,
    INVALID_PRECISION,
    ROUNDING_NECESSARY,
    OVERFLOW,
    METHOD_NOT_ALLOWED,
    PAYLOAD_TOO_LARGE,
    INTERNAL
}
