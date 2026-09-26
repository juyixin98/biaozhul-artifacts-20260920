package com.dstexp.model;

/**
 * Error response envelope, serialized as JSON with HTTP-style {@code code} and HTTP status 400.
 */
public record ErrorResponse(boolean success, String code, String message) {

    public static ErrorResponse of(String code, String message) {
        return new ErrorResponse(false, code, message);
    }
}
