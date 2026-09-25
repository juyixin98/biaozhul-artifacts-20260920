package dev.timeprecision;

/** Domain failure carrying a stable {@link ErrorCode} for JSON error responses. */
public final class ConversionException extends RuntimeException {

    private final ErrorCode code;

    public ConversionException(ErrorCode code, String message) {
        super(message);
        this.code = code;
    }

    public ErrorCode code() {
        return code;
    }
}
