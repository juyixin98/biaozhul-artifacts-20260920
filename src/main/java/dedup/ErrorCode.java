package dedup;

/**
 * Error codes returned by the HTTP API. Each maps to a specific HTTP status.
 */
public enum ErrorCode {
    NOT_FOUND(404),
    PARTITION_EXISTS(409),
    INVALID_BODY(400),
    INVALID_ROUTING_VERSION(409),
    PARTITION_MIGRATING(409),
    WATERMARK_MONOTONIC(409),
    METHOD_NOT_ALLOWED(405),
    INTERNAL(500);

    private final int httpStatus;

    ErrorCode(int httpStatus) {
        this.httpStatus = httpStatus;
    }

    public int httpStatus() {
        return httpStatus;
    }
}
