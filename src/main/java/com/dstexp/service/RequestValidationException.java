package com.dstexp.service;

/**
 * Thrown for malformed client requests; maps to a 400-style JSON error response.
 */
public class RequestValidationException extends RuntimeException {

    private final String code;

    public RequestValidationException(String code, String message) {
        super(message);
        this.code = code;
    }

    public String code() {
        return code;
    }
}
