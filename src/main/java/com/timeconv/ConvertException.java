package com.timeconv;

/** Domain exception carrying a stable machine-readable error code. */
public class ConvertException extends RuntimeException {
    private final ErrorCode code;

    public ConvertException(ErrorCode code, String message) {
        super(message);
        this.code = code;
    }

    public ErrorCode code() {
        return code;
    }
}
